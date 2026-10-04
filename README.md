# Certify.co — Streamlit

Streamlit version of Certify.co using the existing Python verification engine.

## Local run

PowerShell:

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
streamlit run streamlit_app.py
```

Open the Streamlit URL, normally http://localhost:8501.

## Streamlit Community Cloud

1. Create a Streamlit app from this GitHub repository.
2. Set the main file to `streamlit_app.py`.
3. Dependencies are read from `requirements.txt`; Linux packages are read from `packages.txt`.

The app prepares the Playwright Chromium browser on first verification if it is not already present.

## Included

- PDF/image upload
- QR-code and verification-link detection
- OCR extraction
- Browser verification
- Field-by-field comparison
- Issue-date comparison
- Verified / Needs Review / Not Verified / Ignored classification
- Verification history
- Mandatory faculty review remarks
- Manual approval
- CSV export as `certify.co-history.csv`

The existing verification engine remains under `backend/app/main.py`.
