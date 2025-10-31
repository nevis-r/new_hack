import pikepdf
from lxml import etree # Required for the XFA PDF modification logic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from google import genai
import os
import sys
import time # Used for the exponential backoff retry logic
from io import BytesIO

# NOTE: Assuming system_prompts.py is available in the same directory
from system_prompts import SYSTEM_PROMPT_1 

# --- MODULE LEVEL CONFIGURATION (Reads variables directly from Vercel env) ---
# Vercel injects environment variables directly into os.environ
API_KEY = os.environ.get("API_KEY")
MODEL = os.environ.get("MODEL", "gemini-2.5-flash")
# -----------------------------------------------------------------------------

# --- FASTAPI APP INITIALIZATION ---\
app = FastAPI(
    title="DAF 1206 Award Writer (FastAPI)",
    description="Generates DAF 1206 PDF using Gemini AI.",
    version="1.0.0"
)

# Set up Jinja2 templates for HTML rendering (looks for 'templates' directory)
# NOTE: You must ensure your 'index.html' is in a folder named '.' or root
templates = Jinja2Templates(directory=".")

# --- ROBUST FILE PATH RESOLUTION ---\
# Path to the official PDF template
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# NOTE: Adjusted the path assuming 'official.pdf' is in the root where main.py runs, 
# or in a place accessible to the Vercel deployment structure. If it's in 'src/', adjust this.
PDF_TEMPLATE_PATH = os.path.join(BASE_DIR, "official.pdf")

# Pydantic model for input data validation (assuming it was defined near the top)
class FormData(BaseModel):
    # Nomination Data
    award: str
    category: str
    period: str
    agency: str # majcom_foa_dru in the old map
    
    # Nominee Data
    nom_rank: str
    nom_first_name: str
    nom_middle_initial: str = ""
    nom_last_name: str
    duty_title: str # DAFSC in the old map
    nom_telephone: str
    address: str # officeAddress in the old map

    # Commander Data
    com_rank: str
    com_first_name: str
    com_middle_initial: str = ""
    com_last_name: str
    com_telephone: str

class AwardWriter:
    """Writes awards in the DAF 1206 format."""

    def __init__(self, template_path: str = PDF_TEMPLATE_PATH, api_key: str = API_KEY, model: str = MODEL):
        self.template_path = template_path
        self.API_KEY = api_key
        self.MODEL = model
        
        if not self.API_KEY:
            # In Vercel, API_KEY is set via environment variable
            raise ValueError("API_KEY not found in environment variables.")
        self.client = genai.Client(api_key=self.API_KEY)

    def query_api(self, user_prompt: str, max_retries: int = 3) -> str:
        """
        Query Gemini for text to put into the accomplishments section of the DAF1206 form,
        with exponential backoff.
        """
        full_prompt = f"{SYSTEM_PROMPT_1}\n\n{user_prompt}"

        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.MODEL,
                    contents=full_prompt,
                )
                
                if response.text:
                    return response.text
                
                # If no text is returned, treat as a failure for retry
                raise Exception("API returned an empty response text.")

            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt  # 1, 2, 4 seconds
                    # print(f"API call failed (Attempt {attempt+1}/{max_retries}): {e}. Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                else:
                    # print(f"API call failed after {max_retries} attempts.")
                    raise RuntimeError(f"Failed to generate content from AI: {e}")

        # Should be unreachable
        raise RuntimeError("Unexpected failure during AI query.")

    def check_length(self, accomplishments: str) -> tuple[str, str]:
        """
        Splits the accomplishments into two pages, enforcing a 30-line limit per page.
        """
        lines = accomplishments.strip().split('\n')
        MAX_LINES_PER_PAGE = 30 

        page_1_lines = lines[:MAX_LINES_PER_PAGE]
        page_2_lines = lines[MAX_LINES_PER_PAGE:]

        accomplishments_1 = '\n'.join(page_1_lines)
        accomplishments_2 = '\n'.join(page_2_lines)

        return accomplishments_1, accomplishments_2

    def write_pdf(self, data: FormData, accomplishments_1: str, accomplishments_2: str, output_buffer: BytesIO):
        """
        Fills the DAF 1206 PDF (XFA logic) with data and writes to an in-memory buffer.
        """
        
        if not os.path.exists(self.template_path):
            raise ValueError(f"PDF template file not found at: {self.template_path}")

        try:
            with pikepdf.open(self.template_path) as pdf:

                # --- STEP 1: Remove PDF Document Security and Encryption ---
                pdf.security = None

                acroform = pdf.Root.get("/AcroForm", None)
                if acroform is None:
                    raise ValueError("No AcroForm found in this PDF")

                xfa = acroform.get("/XFA", None)
                if xfa is None:
                    raise ValueError("No XFA data found in AcroForm")

                # Locating the dataset of the XFA
                datasets_xml = None
                for i in range(0, len(xfa), 2):
                    name = xfa[i]
                    stream = xfa[i + 1]
                    if name == b"datasets":
                        datasets_xml = stream.read_bytes().decode("utf-8")
                        break

                if datasets_xml is None:
                    raise ValueError("No datasets section found in XFA")

                # To parse and modify the XML
                parser = etree.XMLParser(encoding='utf-8')
                root = etree.fromstring(datasets_xml.encode("utf-8"), parser=parser)
                
                # --- Map form data to XFA fields (using data from the Pydantic model) ---
                data_map = {
                    "award": data.award,
                    "category": data.category,
                    "nomineeTelephone": data.nom_telephone,
                    "awardPeriod": data.period,
                    "majcom_foa_dru": data.agency,
                    
                    # Combine fields for Rank/Name blocks
                    "rankName": f"{data.nom_rank} {data.nom_first_name} {data.nom_middle_initial if data.nom_middle_initial else ''} {data.nom_last_name}",
                    
                    "DAFSC": data.duty_title,
                    "officeAddress": data.address,
                    
                    # Commander data
                    "rank": f"{data.com_rank} {data.com_first_name} {data.com_middle_initial if data.com_middle_initial else ''} {data.com_last_name} {data.com_telephone}",
                    
                    "specificAccomplishments": accomplishments_1,
                    
                    # Page 2 Header (Nominee Name/Rank)
                    "p2Name": f"{data.nom_rank} {data.nom_first_name} {data.nom_middle_initial if data.nom_middle_initial else ''} {data.nom_last_name}",
                    "p2SpecificAccomplishments": accomplishments_2
                }

                # Update each field
                for elem in root.iter():
                    if elem.tag in data_map:
                        elem.text = data_map[elem.tag]

                # Convert back to bytes
                new_datasets = etree.tostring(root, encoding="utf-8", xml_declaration=False)

                # Replace the datasets stream in the PDF
                for i in range(0, len(xfa), 2):
                    name = xfa[i]
                    stream = xfa[i + 1]

                    if name == b"datasets":
                        stream.write(new_datasets)
                        break

                # Save to the in-memory buffer
                pdf.save(output_buffer)
                    
        except Exception as e:
            raise RuntimeError(f"Error while processing PDF form: {e}")

# --- HELPER FUNCTION FOR SHARED LOGIC ---

async def _process_pdf_request(request: Request, award_writer: AwardWriter) -> tuple[BytesIO, str]:
    """Handles common logic: form data extraction, AI query, PDF generation."""
    form_data_dict = dict(await request.form())
    
    # Pydantic validation:
    try:
        data = FormData(**form_data_dict)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Validation Error: {e}")
    
    user_prompt = form_data_dict.get('accomplishments_raw', '')
    if not user_prompt:
         raise HTTPException(status_code=400, detail="Raw accomplishments text is required.")

    # Query AI
    accomplishments = award_writer.query_api(user_prompt)
    
    # Check length and split
    accomplishments_1, accomplishments_2 = award_writer.check_length(accomplishments)

    # Fill the PDF form and save to an in-memory buffer
    pdf_buffer = BytesIO()
    award_writer.write_pdf(data, accomplishments_1.strip(), accomplishments_2.strip(), pdf_buffer)
    pdf_buffer.seek(0) # Rewind the buffer to the beginning
    
    file_name = f"DAF1206_{data.nom_last_name or 'Award'}.pdf"
    
    return pdf_buffer, file_name


# --- ENDPOINTS ---

@app.get("/", response_class=HTMLResponse)
async def serve_form(request: Request):
    """Serve the main HTML form."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/generate_pdf")
async def generate_pdf(request: Request):
    """Handles form submission and forces a file download."""
    try:
        award_writer = AwardWriter()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Configuration Error: {e}")

    try:
        pdf_buffer, file_name = await _process_pdf_request(request, award_writer)
        
        # This is the download response (Content-Disposition: attachment)
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={
                # CRITICAL: Forces the browser to download the file
                "Content-Disposition": f"attachment; filename={file_name}" 
            }
        )

    except (ValueError, RuntimeError, HTTPException) as e:
        # Re-raise exceptions caught in the helper function
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Processing Error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")


@app.post("/preview_pdf")
async def preview_pdf(request: Request):
    """Handles form submission and displays the file inline in the browser."""
    try:
        award_writer = AwardWriter()
    except ValueError as e:
        raise HTTPException(status_code=500, detail=f"Configuration Error: {e}")

    try:
        pdf_buffer, _ = await _process_pdf_request(request, award_writer)
        
        # This is the inline view response (Content-Disposition: inline or omitted)
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={
                # Omit Content-Disposition or set to 'inline' to view in browser
                "Content-Disposition": "inline"
            }
        )

    except (ValueError, RuntimeError, HTTPException) as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(status_code=500, detail=f"Processing Error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")

# This is important for Vercel
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)