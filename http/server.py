"""FastAPI server for Docling document conversion API.

This server provides a single endpoint `/document/convert` that accepts
multipart form data with file attachments and converts each file using Docling.
"""

import json
import logging
import os
import sys
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Add parent directory to path to use local docling module
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.document_converter import (
    DocumentConverter,
    PdfFormatOption,
    WordFormatOption,
)
from docling.pipeline.simple_pipeline import SimplePipeline
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

from docling.datamodel.pipeline_options import (
    PdfPipelineOptions,
    TableStructureOptions,
    TesseractCliOcrOptions,
    AwsTextractOcrOptions
)

# Configure logging
logging.basicConfig(level=logging.INFO)
_log = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB in bytes
MAX_FILES = 10  # Maximum number of files per request


class OutputFormat(str, Enum):
    """Supported output formats for document conversion."""

    JSON = "json"
    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"
    DOCTAGS = "doctags"


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


class ConversionOptions(BaseModel):
    """Options for document conversion.

    This model is extensible for future options.
    Note: output_format is now determined by the Accept header, not options.
    """
    pass


class ErrorResponse(BaseModel):
    """Error information in conversion result."""

    component_type: str
    module_name: str
    error_message: str


class ConversionResultResponse(BaseModel):
    """Response model for a single conversion result."""

    filename: str
    status: str
    document: Optional[dict[str, Any]] = None
    errors: list[ErrorResponse] = []
    output: Optional[str] = Field(
        None, description="Converted document in the requested output format"
    )
    output_format: Optional[str] = Field(
        None, description="The output format used for the 'output' field"
    )


def parse_accept_header(accept: Optional[str]) -> OutputFormat:
    """Parse Accept header to determine output format.
    
    Returns OutputFormat based on Accept header:
    - text/markdown -> MARKDOWN
    - text/html -> HTML
    - text/plain -> TEXT
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
    files: list[UploadFile] = File(..., description="Array of file attachments to convert"),
    options: Optional[str] = Form(
        None, description="JSON object with conversion options (optional, reserved for future use)"
    ),
):
    """Convert multiple documents using Docling.

    This endpoint accepts multipart form data with:
    - `files`: An array of file attachments to convert (max 10 files, 100MB each)
    - `options`: An optional JSON string with conversion options

    Returns an array of conversion results, one for each input file.
    """
    # Debug: Log all received files
    print(f"DEBUG PRINT: Received {len(files)} file(s)")
    for i, file in enumerate(files):
        print(f"DEBUG PRINT: File {i}: {file.filename}, content_type: {file.content_type}")
    
    if not files:
        raise HTTPException(status_code=400, detail="At least one file must be provided")

    # Validate file count
    if len(files) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum {MAX_FILES} files allowed per request.",
        )

    # Debug: Print to console (user's debug statement)
    print(f"DEBUG PRINT: Options received: {repr(options)}")
    print(f"DEBUG PRINT: Options type: {type(options)}")
    
    # Debug: Log raw options value
    _log.info(f"DEBUG: Raw options received: {repr(options)}")
    _log.info(f"DEBUG: Options type: {type(options)}")
    if options:
        _log.info(f"DEBUG: Options length: {len(options)}")
        _log.info(f"DEBUG: Options first 100 chars: {repr(options[:100])}")
        print(f"DEBUG PRINT: Options length: {len(options)}")
        print(f"DEBUG PRINT: Options first 100 chars: {repr(options[:100])}")
    
    # Parse options if provided
    conversion_options = ConversionOptions()  # Use defaults
    if options:
        try:
            # Handle both cases:
            # 1. Postman/curl with quotes: options="{\"key\": \"value\"}" -> string needs unquoting
            # 2. Direct JSON: options={"key": "value"} -> direct JSON parsing
            options_str = options.strip()
            _log.info(f"DEBUG: Options after strip: {repr(options_str)}")
            
            # Try parsing directly first (curl case: options={"key": "value"})
            try:
                options_dict = json.loads(options_str)
                _log.info(f"DEBUG: Direct JSON parse succeeded: {options_dict}")
            except json.JSONDecodeError as e:
                _log.info(f"DEBUG: Direct JSON parse failed: {e}")
                # If that fails, check if it's wrapped in quotes (Postman case)
                # Postman sends: options="{\"key\": \"value\"}"
                if (options_str.startswith('"') and options_str.endswith('"')) or (
                    options_str.startswith("'") and options_str.endswith("'")
                ):
                    _log.info("DEBUG: Detected quoted string, unquoting...")
                    # Remove outer quotes
                    options_str = options_str[1:-1]
                    _log.info(f"DEBUG: After removing quotes: {repr(options_str)}")
                    # Unescape escaped quotes and backslashes
                    # Handle \" -> " and \\ -> \
                    options_str = options_str.replace('\\"', '"').replace('\\\\', '\\')
                    _log.info(f"DEBUG: After unescaping: {repr(options_str)}")
                    # Try parsing again
                    options_dict = json.loads(options_str)
                    _log.info(f"DEBUG: JSON parse after unquoting succeeded: {options_dict}")
                else:
                    _log.error(f"DEBUG: Not a quoted string and JSON parse failed")
                    # Re-raise the original error if it's not a quoting issue
                    raise
            
            # Note: output_format is now determined by Accept header, not options
            # Options are reserved for future conversion settings
            conversion_options = ConversionOptions(**options_dict)
            _log.info(f"DEBUG: Final conversion_options: {conversion_options}")
        except json.JSONDecodeError as e:
            _log.error(f"DEBUG: JSON decode error: {e}")
            raise HTTPException(
                status_code=400, detail=f"Invalid JSON in options parameter: {str(e)}"
            )
        except HTTPException:
            raise  # Re-raise HTTP exceptions
        except Exception as e:
            _log.error(f"DEBUG: Unexpected error parsing options: {e}", exc_info=True)
            raise HTTPException(
                status_code=400, detail=f"Invalid options format: {str(e)}"
            )
    else:
        _log.info("DEBUG: No options provided, using defaults")

    # Determine output format from Accept header
    accept_header = request.headers.get("Accept")
    output_format = parse_accept_header(accept_header)
    _log.info(f"DEBUG: Accept header: {accept_header}, output_format: {output_format.value}")
    print(f"DEBUG PRINT: Accept header: {accept_header}, output_format: {output_format.value}")

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

        
        # Initialize DocumentConverter
        # Create pipeline options with AWS Textract OCR configuration
        pdf_pipeline_options = PdfPipelineOptions(
            do_ocr=False,
            do_table_structure=True,
            table_structure_options=TableStructureOptions(
                do_cell_matching=True
            ),
            ocr_options=AwsTextractOcrOptions(
                force_full_page_ocr=True,
                region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
            )
        )

        doc_converter = DocumentConverter(  # all of the below is optional, has internal defaults.
            allowed_formats=[
                InputFormat.PDF,
                InputFormat.IMAGE,
                InputFormat.DOCX,
                InputFormat.HTML,
                InputFormat.PPTX,
                InputFormat.ASCIIDOC,
                InputFormat.CSV,
                InputFormat.MD,
                InputFormat.XLSX,
            ],  # whitelist formats, non-matching files are ignored.
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_cls=StandardPdfPipeline,
                    backend=PyPdfiumDocumentBackend,
                    pipeline_options=pdf_pipeline_options,
                ),
                InputFormat.DOCX: WordFormatOption(
                    pipeline_cls=SimplePipeline  # or set a backend, e.g., MsWordDocumentBackend
                    # If you change the backend, remember to import it, e.g.:
                    #   from docling.backend.msword_backend import MsWordDocumentBackend
                ),
            },
        )

        # Convert all files
        results = []
        try:
            conv_results = doc_converter.convert_all(file_paths)

            for res in conv_results:
                # Extract filename from input
                filename = "unknown"
                if hasattr(res.input, "file") and res.input.file:
                    # file is a PurePath, so we can get the name
                    filename = str(res.input.file.name) if res.input.file.name else str(res.input.file)

                # Extract relevant information from ConversionResult
                status_value = res.status.value if hasattr(res.status, "value") else str(res.status)
                result_data = {
                    "filename": filename,
                    "status": status_value,
                    "errors": [
                        {
                            "component_type": (
                                error.component_type.value
                                if hasattr(error.component_type, "value")
                                else str(error.component_type)
                            ),
                            "module_name": error.module_name,
                            "error_message": error.error_message,
                        }
                        for error in res.errors
                    ],
                }

                # Include document if conversion was successful
                if res.document and status_value == "success":
                    try:
                        # Generate output in the requested format
                        if output_format == OutputFormat.JSON:
                            # For JSON, include the document dict
                            result_data["document"] = res.document.export_to_dict()
                            result_data["output"] = None
                            result_data["output_format"] = output_format.value
                        else:
                            # For non-JSON formats, generate the output content
                            # Store it in result_data for potential JSON fallback, but we'll return it directly
                            if output_format == OutputFormat.MARKDOWN:
                                result_data["output"] = res.document.export_to_markdown()
                                _log.debug(f"Generated markdown output for {filename}")
                            elif output_format == OutputFormat.HTML:
                                # save_as_html writes to a file, so we use a temporary file
                                with tempfile.NamedTemporaryFile(
                                    mode="w", suffix=".html", delete=False
                                ) as tmp_file:
                                    tmp_path = Path(tmp_file.name)
                                    res.document.save_as_html(
                                        filename=tmp_path, image_mode=ImageRefMode.PLACEHOLDER
                                    )
                                    result_data["output"] = tmp_path.read_text(encoding="utf-8")
                                    tmp_path.unlink()  # Clean up
                            elif output_format == OutputFormat.TEXT:
                                result_data["output"] = res.document.export_to_markdown(
                                    strict_text=True
                                )
                            elif output_format == OutputFormat.DOCTAGS:
                                result_data["output"] = res.document.export_to_doctags()
                            
                            # For non-JSON, we still include document for error cases
                            result_data["document"] = res.document.export_to_dict()
                            result_data["output_format"] = output_format.value
                            
                        _log.info(
                            f"DEBUG: Exporting document {filename} in format: {output_format.value} (enum: {output_format})"
                        )
                        print(f"DEBUG PRINT: Exporting document {filename} in format: {output_format.value}")

                    except Exception as e:
                        _log.error(f"Error exporting document for {filename}: {e}")
                        result_data["errors"].append(
                            {
                                "component_type": "api",
                                "module_name": "server",
                                "error_message": f"Error exporting document: {str(e)}",
                            }
                        )

                results.append(ConversionResultResponse(**result_data))

        except Exception as e:
            _log.error(f"Error during conversion: {e}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"Conversion error: {str(e)}"
            )

    # Return response based on Accept header
    if output_format == OutputFormat.JSON:
        # Return JSON structure
        return results
    else:
        # For non-JSON formats, return the content directly
        # If multiple files, concatenate with separators
        if len(results) == 0:
            raise HTTPException(status_code=500, detail="No conversion results")
        
        # Extract the output content from results
        # results is a list of ConversionResultResponse objects
        output_parts = []
        for result in results:
            if result.status != "success":
                # If conversion failed, include error info
                error_msg = f"Error converting {result.filename}: {', '.join([e.error_message for e in result.errors])}"
                if output_format == OutputFormat.MARKDOWN:
                    output_parts.append(f"# Error: {error_msg}\n")
                elif output_format == OutputFormat.HTML:
                    output_parts.append(f"<h1>Error: {result.filename}</h1><p>{error_msg}</p>\n")
                else:
                    output_parts.append(f"Error: {error_msg}\n")
            else:
                # Get the output content
                output_content = result.output
                if not output_content:
                    output_content = "(No output available)"
                
                if output_format == OutputFormat.MARKDOWN:
                    output_parts.append(f"# {result.filename}\n\n{output_content}\n")
                elif output_format == OutputFormat.HTML:
                    output_parts.append(f"<h1>{result.filename}</h1>\n{output_content}\n")
                elif output_format == OutputFormat.TEXT:
                    output_parts.append(f"{result.filename}\n{'=' * len(result.filename)}\n\n{output_content}\n\n")
                elif output_format == OutputFormat.DOCTAGS:
                    output_parts.append(f"# {result.filename}\n{output_content}\n")
        
        # Join all parts with separator
        if len(output_parts) > 1:
            separator = "\n" + ("=" * 80) + "\n\n"
            content = separator.join(output_parts)
        else:
            content = output_parts[0] if output_parts else ""
        
        # Return with appropriate content type
        if output_format == OutputFormat.MARKDOWN:
            return PlainTextResponse(content=content, media_type="text/markdown")
        elif output_format == OutputFormat.HTML:
            return HTMLResponse(content=content)
        elif output_format == OutputFormat.TEXT:
            return PlainTextResponse(content=content, media_type="text/plain")
        elif output_format == OutputFormat.DOCTAGS:
            return PlainTextResponse(content=content, media_type="text/plain")
        else:
            # Fallback to JSON
            return results


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
