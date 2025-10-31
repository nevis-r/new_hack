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

# --- FASTAPI APP INITIALIZATION ---
app = FastAPI(
    title="DAF 1206 Award Writer (FastAPI)",
    description="Generates DAF 1206 PDF using Gemini AI.",
    version="1.0.0"
)

# Set up Jinja2 templates for HTML rendering (looks for 'templates' directory)
# NOTE: You must ensure your 'index.html' is in same directory
templates = Jinja2Templates(directory=".")

# --- ROBUST FILE PATH RESOLUTION ---
# Path to the official PDF template
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PDF_TEMPLATE_PATH = os.path.join(BASE_DIR, "official.pdf")
# --- END OF ROBUST FILE PATH RESOLUTION ---

# --- PYDANTIC MODEL FOR FORM DATA ---
# Defines the structure and data types for the incoming web form data.
class FormData(BaseModel):
    # AI Prompt & Meta
    ai_prompt: str
    award: str
    category: str

    # Nominee Data
    nom_rank: str
    nom_first_name: str
    nom_middle_initial: str | None
    nom_last_name: str
    duty_title: str
    agency: str
    period: str
    address: str
    nom_telephone: str

    # Commander Data
    com_rank: str
    com_first_name: str
    com_middle_initial: str | None
    com_last_name: str
    com_telephone: str

# --- AWARD WRITER CLASS ---

class AwardWriter:
    """Writes awards in the DAF 1206 format."""

    def __init__(self, template_path=PDF_TEMPLATE_PATH):
        self.template_path = template_path
        
        if not os.path.exists(self.template_path):
             # Raise an error if the PDF template is missing
             raise ValueError(f"Template file not found at: {self.template_path}")

        self.MODEL = MODEL
        
        # Gracefully handle missing API key
        if API_KEY:
            self.client = genai.Client(api_key=API_KEY)
        else:
            self.client = None
            print("FATAL ERROR: API_KEY not set. AI functions will return error message.", file=sys.stderr)


    # NOTE: This is a synchronous function, but FastAPI will run it in a threadpool 
    # because the main route is async. This is necessary due to the blocking time.sleep().
    def query_api(self, user_prompt: str) -> str:
        """Query Gemini for text using exponential backoff retry logic."""
        
        # Handle Missing Key Gracefully
        if not self.client:
            print("[ERROR] API Client not initialized due to missing API_KEY.")
            return "Error: API_KEY is missing. Please configure environment variables."
        
        full_prompt = f"{SYSTEM_PROMPT_1}\n\n{user_prompt}"
        max_retries = 5
        base_delay = 1 # seconds

        for attempt in range(max_retries):
            try:
                response = self.client.models.generate_content(
                    model=self.MODEL,
                    contents=full_prompt,
                )
                
                if response.text:
                    return response.text.strip()
                else:
                    print(f"[WARNING] Attempt {attempt+1}: AI returned no text content.")
                    if attempt == max_retries - 1:
                        return "Error: AI returned no accomplishment text after multiple retries."

            except Exception as e:
                error_message = str(e)
                # Check for 503 UNAVAILABLE or other transient errors
                if attempt < max_retries - 1 and ("503 UNAVAILABLE" in error_message or "Internal Server Error" in error_message):
                    delay = base_delay * (2 ** attempt)
                    print(f"[WARNING] Attempt {attempt+1} failed ({e}). Retrying in {delay:.2f}s...")
                    time.sleep(delay)
                else:
                    print(f"[ERROR] AI API call failed definitively: {e}")
                    return f"Error querying AI: {e}"

        return "Error: Failed to query AI after maximum retries."

    def check_length(self, accomplishments):
        """Checks for the 'BREAK' keyword and splits the accomplishments text into two paragraphs."""
        
        break_keyword = "BREAK"
        
        # 1. Check for explicit "BREAK" keyword
        if break_keyword in accomplishments:
            parts = accomplishments.split(break_keyword, 1)
            accomplishments_1 = parts[0].strip()
            accomplishments_2 = parts[1].strip()
            
            if accomplishments_1 and accomplishments_2:
                return accomplishments_1, accomplishments_2

        return accomplishments_1, accomplishments_2

    def write_pdf(self, data: FormData, accomplishments_1: str, accomplishments_2: str, output_buffer: BytesIO):
        """
        Fills the DAF 1206 PDF (XFA logic) with data and writes to an in-memory buffer.
        
        :param data: The validated form data (Pydantic model).
        :param accomplishments_1: Text for the first accomplishments page.
        :param accomplishments_2: Text for the second accomplishments page.
        :param output_buffer: The BytesIO buffer to write the final PDF to.
        """
        
        # Ensure the template file exists before attempting to open
        if not os.path.exists(self.template_path):
             raise ValueError(f"PDF template file not found at: {self.template_path}")

        try:
            with pikepdf.open(self.template_path) as pdf:
                # --- NEW: Remove Security Restrictions (Passwords, Permissions) ---
                # This ensures the final PDF is fully editable, printable, and copyable.
                pdf.remove_security()

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
                root = etree.fromstring(datasets_xml.encode("utf-8"))
                
                # --- Map form data to XFA fields (using data from the Pydantic model) ---
                data_map = {
                    "award": data.award,
                    "category": data.category,
                    "nomineeTelephone": data.nom_telephone,
                    "awardPeriod": data.period,
                    "majcom_foa_dru": data.agency,
                    
                    # Combine fields for Rank/Name blocks as in the Flask version
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
            # Catch errors during PDF processing and re-raise as a RuntimeError
            raise RuntimeError(f"Error while processing PDF form: {e}")

# --- GLOBAL INITIALIZATION ---
# This attempts to initialize the writer once on startup
try:
    award_writer = AwardWriter()
except ValueError as e:
    print(f"CRITICAL SETUP ERROR: {e}", file=sys.stderr)
    award_writer = None 

# --- FASTAPI ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Renders the HTML form for input."""
    if award_writer is None:
        raise HTTPException(status_code=503, detail="Application is not configured correctly (API Key or PDF template missing).")
    
    # Use Jinja2Templates to render the HTML
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/generate")
async def generate_award(request: Request):
    """
    Generates the award text, fills the PDF, and returns the file.
    
    This replaces the Flask generate_award function.
    """
    if award_writer is None:
        raise HTTPException(status_code=503, detail="Application is not configured correctly.")

    try:
        # 1. Get data from the web form using FastAPI's Request object
        form_data = await request.form()
        
        # 2. Convert form data to our Pydantic model for structure and validation
        # We manually handle optional fields that might be missing or empty strings
        data = FormData(
            ai_prompt=form_data.get('ai_prompt'),
            award=form_data.get('award'),
            category=form_data.get('category'),
            nom_rank=form_data.get('nom_rank'),
            nom_first_name=form_data.get('nom_first_name'),
            nom_middle_initial=form_data.get('nom_middle_initial') or None, # Treat empty string as None
            nom_last_name=form_data.get('nom_last_name'),
            duty_title=form_data.get('duty_title'),
            agency=form_data.get('agency'),
            period=form_data.get('period'),
            address=form_data.get('address'),
            nom_telephone=form_data.get('nom_telephone'),
            com_rank=form_data.get('com_rank'),
            com_first_name=form_data.get('com_first_name'),
            com_middle_initial=form_data.get('com_middle_initial') or None, # Treat empty string as None
            com_last_name=form_data.get('com_last_name'),
            com_telephone=form_data.get('com_telephone'),
        )
        
        # 3. Query the Gemini API for accomplishments (this is run in a threadpool)
        user_prompt = data.ai_prompt or 'Write an award for an outstanding Airman.'
        accomplishments = award_writer.query_api(user_prompt)

        if accomplishments.startswith("Error:"):
             raise RuntimeError(f"AI Query Failed: {accomplishments}")
             
        # 4. Check length and split
        accomplishments_1, accomplishments_2 = award_writer.check_length(accomplishments)

        # 5. Fill the PDF form and save to an in-memory buffer
        pdf_buffer = BytesIO()
        # The function signature now matches the in-memory write pattern
        award_writer.write_pdf(data, accomplishments_1.strip(), accomplishments_2.strip(), pdf_buffer)
        pdf_buffer.seek(0) # Rewind the buffer to the beginning

        # 6. Return the file using StreamingResponse (replaces Flask's send_file)
        file_name = f"DAF1206_{data.nom_last_name or 'Award'}.pdf"
        
        return StreamingResponse(
            pdf_buffer,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename={file_name}"
            }
        )

    except ValueError as e:
        # Handle Pydantic validation failures or configuration errors
        raise HTTPException(status_code=400, detail=f"Input/Configuration Error: {e}")
    except RuntimeError as e:
        # Handle AI query or PDF processing errors
        raise HTTPException(status_code=500, detail=f"Processing Error: {e}")
    except Exception as e:
        # Catch other errors
        raise HTTPException(status_code=500, detail=f"An unexpected error occurred: {e}")

# This is important for Vercel
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)