# Docling HTTP API Server

A FastAPI-based HTTP server for document conversion using Docling.

## Installation

This server requires FastAPI and uvicorn. Install them along with Docling:

```bash
pip install fastapi uvicorn[standard]
```

Or if you're using the project's dependencies:

```bash
pip install fastapi uvicorn[standard] docling
```

## Running the Server

Run the server directly:

```bash
python http/server.py
```

Or using uvicorn:

```bash
uvicorn http.server:app --host 0.0.0.0 --port 8000
```

The server will start on `http://0.0.0.0:8000` by default.

## API Endpoints

### POST `/document/convert`

Convert one or more documents using Docling.

**Request:**
- Content-Type: `multipart/form-data`
- Accept: Determines the response format (see Output Formats below)
- Parameters:
  - `files`: Array of file attachments (required, max 10 files, 100MB each)
  - `options`: JSON string with conversion options (optional, reserved for future use)

**Output Formats (via Accept header):**

The response format is determined by the `Accept` header:

- `application/json` or `*/*` or no header → Returns JSON structure (default)
- `text/markdown` → Returns raw Markdown content
- `text/html` → Returns raw HTML content
- `text/plain` → Returns plain text content
- `text/doctags` → Returns Doctags format content

**JSON Response (default):**

When `Accept: application/json` or no Accept header is provided, returns a JSON array with conversion results:

```json
[
  {
    "filename": "document.pdf",
    "status": "success",
    "document": { ... },
    "output": null,
    "output_format": "json",
    "errors": []
  }
]
```

**Raw Content Response:**

When a non-JSON Accept header is provided (e.g., `text/markdown`), returns the raw content directly with the appropriate `Content-Type` header. For multiple files, outputs are concatenated with separators.

**Limits:**
- Maximum 10 files per request
- Maximum 100MB per file
- Files exceeding these limits will result in a 400 error

**Examples:**

**JSON output (default):**

```bash
curl -X POST "http://localhost:8000/document/convert" \
  -F "files=@document1.pdf" \
  -F "files=@document2.docx"
```

**Markdown output:**

```bash
curl -X POST "http://localhost:8000/document/convert" \
  -H "Accept: text/markdown" \
  -F "files=@document.pdf"
```

**HTML output:**

```bash
curl -X POST "http://localhost:8000/document/convert" \
  -H "Accept: text/html" \
  -F "files=@document.pdf"
```

**Plain text output:**

```bash
curl -X POST "http://localhost:8000/document/convert" \
  -H "Accept: text/plain" \
  -F "files=@document.pdf"
```

**Multiple files with markdown:**

```bash
curl -X POST "http://localhost:8000/document/convert" \
  -H "Accept: text/markdown" \
  -F "files=@document1.pdf" \
  -F "files=@document2.docx"
```

**Example using Python requests:**

```python
import requests

files = [
    ('files', ('document1.pdf', open('document1.pdf', 'rb'), 'application/pdf')),
    ('files', ('document2.docx', open('document2.docx', 'rb'), 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'))
]

# JSON output (default)
response = requests.post('http://localhost:8000/document/convert', files=files)
results = response.json()

for result in results:
    print(f"File: {result['filename']}, Status: {result['status']}")
    if result['document']:
        print(f"Document converted successfully")

# Markdown output
headers = {'Accept': 'text/markdown'}
response = requests.post(
    'http://localhost:8000/document/convert',
    files=files,
    headers=headers
)
markdown_content = response.text
print(markdown_content)

# HTML output
headers = {'Accept': 'text/html'}
response = requests.post(
    'http://localhost:8000/document/convert',
    files=files,
    headers=headers
)
html_content = response.text
print(html_content)
```

**Example using Postman:**

1. Set the request method to `POST`
2. Set the URL to `http://localhost:8000/document/convert`
3. In the **Headers** tab, add:
   - Key: `Accept`
   - Value: `text/markdown` (or `text/html`, `text/plain`, etc.)
4. In the **Body** tab, select `form-data`:
   - Add a field `files` with type `File` and select your PDF/document file
   - Optionally add an `options` field with type `Text` (reserved for future use)

### GET `/health`

Health check endpoint.

**Response:**
```json
{"status": "healthy"}
```

## Notes

- The server uses Docling's default conversion settings
- Files are temporarily stored in a system temp directory during processing
- CORS is enabled for all origins
- Default output format is JSON (when no Accept header or `Accept: */*` is provided)
- For non-JSON formats, the response contains the raw content directly (not wrapped in JSON)
- Multiple files with non-JSON formats are concatenated with separators
- The `options` parameter is reserved for future conversion settings (currently not used)
