"""FastAPI server for Docling document conversion API.

This server provides a single endpoint `/document/convert` that accepts
multipart form data with file attachments and converts each file using Docling.
"""

import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Add parent directory to path to use local docling module
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse

from rest.common import (
    MAX_FILE_SIZE,
    MAX_FILES,
    ConversionOptions,
    OutputFormat,
    convert_files,
    format_conversion_result,
    format_results_for_output,
)

# Configure logging
logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)


app = FastAPI(
    title="Docling Document Conversion API",
    description="API for converting documents using Docling",
    version="1.0.0",
)

# Add CORS middleware - allow all origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def parse_accept_header(accept: Optional[str]) -> OutputFormat:
    """Parse Accept header to determine output format.

    Returns OutputFormat based on Accept header:
    - text/markdown -> MARKDOWN
    - text/html -> HTML
    - text/plain -> TEXT
    - application/x-yaml or text/yaml -> YAML
    - application/json or */* or None -> JSON
    """
    if not accept:
        return OutputFormat.JSON

    accept_lower = accept.lower()

    # Check for specific content types
    if "text/markdown" in accept_lower:
        return OutputFormat.MARKDOWN
    elif "text/html" in accept_lower:
        return OutputFormat.HTML
    elif "text/plain" in accept_lower:
        return OutputFormat.TEXT
    elif "application/x-yaml" in accept_lower or "text/yaml" in accept_lower:
        return OutputFormat.YAML
    elif "application/json" in accept_lower:
        return OutputFormat.JSON
    elif "*/*" in accept_lower:
        return OutputFormat.JSON
    else:
        # Default to JSON if Accept header is not recognized
        return OutputFormat.JSON


@app.post("/document/convert")
async def convert_documents(
    request: Request,
    files: list[UploadFile] = File(
        ..., description="Array of file attachments to convert"
    ),
    options: Optional[str] = Form(
        None,
        description="JSON object with conversion options (optional, reserved for future use)",
    ),
):
    """Convert multiple documents using Docling.

    This endpoint accepts multipart form data with:
    - `files`: An array of file attachments to convert (max 10 files, 100MB each)
    - `options`: An optional JSON string with conversion options

    Returns an array of conversion results, one for each input file.
    """
    if not files:
        raise HTTPException(status_code=400, detail="At least one file must be provided")

    # Validate file count
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_FILES} files allowed per request.",
        )

    # Parse options if provided
    conversion_options = ConversionOptions()  # Use defaults
    if options:
        try:
            options_str = options.strip()

            # Try parsing directly first (curl case: options={"key": "value"})
            try:
                options_dict = json.loads(options_str)
            except json.JSONDecodeError:
                # If that fails, check if it's wrapped in quotes (Postman case)
                if (options_str.startswith('"') and options_str.endswith('"')) or (
                    options_str.startswith("'") and options_str.endswith("'")
                ):
                    # Remove outer quotes
                    options_str = options_str[1:-1]
                    # Unescape escaped quotes and backslashes
                    options_str = options_str.replace('\\"', '"').replace("\\\\", "\\")
                    options_dict = json.loads(options_str)
                else:
                    raise

            conversion_options = ConversionOptions(**options_dict)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid JSON in options parameter: {str(e)}"
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid options format: {str(e)}"
            )

    # Determine output format from Accept header
    accept_header = request.headers.get("Accept")
    output_format = parse_accept_header(accept_header)
    _log.info(f"Accept header: {accept_header}, output_format: {output_format.value}")

    # Create a temporary directory to store uploaded files
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        file_paths = []

        # Save uploaded files to temporary directory
        for file in files:
            if not file.filename:
                continue

            # Read file content
            content = await file.read()
            if not content:
                _log.warning(f"Skipping empty file: {file.filename}")
                continue

            # Validate file size
            file_size = len(content)
            if file_size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"File '{file.filename}' exceeds maximum size of "
                        f"{MAX_FILE_SIZE / (1024 * 1024):.0f}MB. "
                        f"File size: {file_size / (1024 * 1024):.2f}MB"
                    ),
                )

            # Save to temporary file
            file_path = temp_path / file.filename
            file_path.write_bytes(content)
            file_paths.append(file_path)
            _log.info(f"Saved uploaded file: {file.filename} ({file_size} bytes)")

        if not file_paths:
            raise HTTPException(status_code=400, detail="No valid files provided")

        # Convert files using common module
        try:
            raw_results = convert_files(file_paths, conversion_options)
            # Format results for output
            results = [
                format_conversion_result(r, output_format) for r in raw_results
            ]
        except Exception as e:
            _log.error(f"Error during conversion: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Conversion error: {str(e)}")

    # Return response based on Accept header
    if output_format == OutputFormat.JSON:
        return results
    else:
        # For non-JSON formats, return the content directly
        if len(results) == 0:
            raise HTTPException(status_code=500, detail="No conversion results")

        content, media_type = format_results_for_output(results, output_format)

        # Return with appropriate content type
        if output_format == OutputFormat.HTML:
            return HTMLResponse(content=content)
        else:
            return PlainTextResponse(content=content, media_type=media_type)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
