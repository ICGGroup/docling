"""Shared document conversion logic for HTTP server and Lambda handler.

This module contains the core conversion functionality that is shared between
the FastAPI HTTP server and the AWS Lambda handler.
"""

import logging
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, Field

from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import (
    AwsTextractOcrOptions,
    EasyOcrOptions,
    OcrMacOptions,
    PdfPipelineOptions,
    RapidOcrOptions,
    TableFormerMode,
    TableStructureOptions,
    TesseractCliOcrOptions,
    TesseractOcrOptions,
)
from docling.document_converter import (
    DocumentConverter,
    ImageFormatOption,
    PdfFormatOption,
    WordFormatOption,
)
from docling.pipeline.simple_pipeline import SimplePipeline
from docling.pipeline.legacy_standard_pdf_pipeline import LegacyStandardPdfPipeline
from docling.utils.verbose import vprint, vtimer
from docling_core.types.doc import ImageRefMode

# Configure logging
_log = logging.getLogger(__name__)

# Constants
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB in bytes
MAX_FILES = 10  # Maximum number of files per request


class OutputFormat(str, Enum):
    """Supported output formats for document conversion."""

    JSON = "json"
    YAML = "yaml"
    MARKDOWN = "markdown"
    HTML = "html"
    TEXT = "text"
    DOCTAGS = "doctags"


class OcrEngine(str, Enum):
    """Supported OCR engines."""

    NONE = "none"  # No OCR
    RAPIDOCR = "rapidocr"  # RapidOCR (default, fast, supports many languages)
    EASYOCR = "easyocr"  # EasyOCR (GPU-accelerated, many languages)
    TESSERACT = "tesseract"  # Tesseract via tesserocr library
    TESSERACT_CLI = "tesseract_cli"  # Tesseract via CLI
    AWS_TEXTRACT = "textract"  # AWS Textract (cloud-based)
    OCRMAC = "ocrmac"  # macOS native OCR (macOS only)


class OcrOptionsModel(BaseModel):
    """OCR configuration options."""

    engine: OcrEngine = Field(
        default=OcrEngine.NONE,
        description="OCR engine to use. Defaults to 'none' (no OCR).",
    )
    force_full_page_ocr: bool = Field(
        default=False,
        description="If enabled, OCR is applied to the entire page regardless of detected text.",
    )
    bitmap_area_threshold: float = Field(
        default=0.05,
        description="Percentage of page area for a bitmap to trigger OCR processing (0.0-1.0).",
        ge=0.0,
        le=1.0,
    )
    lang: Optional[List[str]] = Field(
        default=None,
        description="List of languages for OCR. Format depends on the engine.",
    )

    # AWS Textract specific options
    aws_region: Optional[str] = Field(
        default=None,
        description="AWS region for Textract (e.g., 'us-east-1'). Uses environment default if not specified.",
    )

    # EasyOCR specific options
    use_gpu: Optional[bool] = Field(
        default=None,
        description="Enable GPU acceleration for EasyOCR.",
    )

    # Tesseract specific options
    tesseract_cmd: Optional[str] = Field(
        default=None,
        description="Path to tesseract executable (for tesseract_cli engine).",
    )
    tesseract_psm: Optional[int] = Field(
        default=None,
        description="Tesseract Page Segmentation Mode (0-13).",
        ge=0,
        le=13,
    )


class PipelineMode(str, Enum):
    """Pipeline implementation to use for PDF processing."""

    LEGACY = "legacy"  # Sequential pipeline (default, fast cold start)
    THREADED = "threaded"  # Multi-threaded pipeline (better for large documents)


class ConversionOptions(BaseModel):
    """Options for document conversion."""

    ocr: Optional[OcrOptionsModel] = Field(
        default=None,
        description="OCR configuration. If not specified, OCR is disabled.",
    )
    pipeline: PipelineMode = Field(
        default=PipelineMode.LEGACY,
        description="Pipeline mode: 'legacy' (default, fast cold start) or 'threaded' (better for large multi-page documents).",
    )
    table_mode: TableFormerMode = Field(
        default=TableFormerMode.FAST,
        description="Table structure mode: 'fast' (default, faster) or 'accurate' (slower ML inference, more precise).",
    )
    do_table_structure: bool = Field(
        default=True,
        description="Enable table structure recognition via TableFormer ML model. When False, skips model loading and inference entirely (saves 3-35s init + 10-40s per table on CPU). Layout model still identifies table regions with concatenated text.",
    )
    document_timeout: Optional[float] = Field(
        default=None,
        description="Maximum time in seconds for document conversion. When exceeded, returns partial results gracefully. None = no timeout.",
    )


class ErrorResponse(BaseModel):
    """Error information in conversion result."""

    component_type: str
    module_name: str
    error_message: str


class ConversionResultResponse(BaseModel):
    """Response model for a single conversion result."""

    filename: str
    status: str
    document: Optional[Dict[str, Any]] = None
    errors: List[ErrorResponse] = []
    output: Optional[str] = Field(
        None, description="Converted document in the requested output format"
    )
    output_format: Optional[str] = Field(
        None, description="The output format used for the 'output' field"
    )
    confidence: Optional[Dict[str, Any]] = Field(
        None, description="Confidence scores and grades for the conversion"
    )


@dataclass
class DoclingConversionResult:
    """Wrapper for raw Docling conversion result.

    This class holds the raw Docling document along with metadata,
    allowing handlers to process the document before formatting.
    """

    filename: str
    status: str
    document: Any = None  # Raw Docling document object
    errors: List[Dict[str, str]] = field(default_factory=list)
    confidence: Optional[Any] = None  # ConfidenceReport from ConversionResult


def normalize_bboxes(doc_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Add normalized coordinates to all bbox objects in AWS Textract format.

    This function:
    - Preserves original l, t, r, b values and coord_origin
    - Adds a 'normalized' element with Left, Top, Width, Height (0.0-1.0)
    - Uses TOPLEFT origin for normalized values (AWS Textract standard)
    - Uses per-page dimensions from the 'pages' dict

    Args:
        doc_dict: The document dictionary from export_to_dict()

    Returns:
        Modified document dictionary with normalized bboxes added
    """
    # Extract page dimensions
    pages = doc_dict.get("pages", {})

    def get_page_size(page_no: Union[int, str]) -> Tuple[float, float]:
        """Get width and height for a given page number."""
        page_key = str(page_no)
        if page_key in pages:
            size = pages[page_key].get("size", {})
            return size.get("width", 612.0), size.get("height", 792.0)
        # Default to standard letter size if page not found
        return 612.0, 792.0

    def add_normalized_bbox(
        bbox: Dict[str, Any], page_width: float, page_height: float
    ) -> Dict[str, Any]:
        """Add normalized coordinates to a bbox dict in AWS Textract format."""
        if not bbox or "l" not in bbox:
            return bbox

        l = bbox.get("l", 0)
        t = bbox.get("t", 0)
        r = bbox.get("r", 0)
        b = bbox.get("b", 0)
        coord_origin = bbox.get("coord_origin", "TOPLEFT")

        # Calculate normalized coordinates in TOPLEFT origin (AWS Textract standard)
        # Horizontal: same for both origins
        norm_left = l / page_width
        norm_width = (r - l) / page_width

        # Vertical: depends on coordinate origin
        if coord_origin == "BOTTOMLEFT":
            # In BOTTOMLEFT: y=0 is at bottom, y=height is at top
            # t is the top (higher y value), b is the bottom (lower y value)
            # Convert to TOPLEFT: y=0 is at top, y=height is at bottom
            norm_top = 1.0 - (t / page_height)  # top of box in TOPLEFT coords
            norm_height = (t - b) / page_height  # height is t - b in BOTTOMLEFT
        else:
            # Already TOPLEFT: y=0 is at top
            # t is the top (lower y value), b is the bottom (higher y value)
            norm_top = t / page_height
            norm_height = (b - t) / page_height

        # Add normalized element while preserving original bbox attributes
        bbox["normalized"] = {
            "Left": norm_left,
            "Top": norm_top,
            "Width": norm_width,
            "Height": norm_height,
        }
        return bbox

    def process_prov_list(prov_list: List[Dict[str, Any]]) -> None:
        """Process a list of provenance entries, adding normalized coords to bboxes."""
        for prov in prov_list:
            if "bbox" in prov and "page_no" in prov:
                page_width, page_height = get_page_size(prov["page_no"])
                add_normalized_bbox(prov["bbox"], page_width, page_height)

    def process_table_cells(cells: List[Dict[str, Any]], page_no: int) -> None:
        """Process table cells, adding normalized coords to bboxes."""
        page_width, page_height = get_page_size(page_no)
        for cell in cells:
            if "bbox" in cell:
                add_normalized_bbox(cell["bbox"], page_width, page_height)

    def process_table_grid(
        grid: List[List[Dict[str, Any]]], page_no: int
    ) -> None:
        """Process table grid, adding normalized coords to bboxes."""
        page_width, page_height = get_page_size(page_no)
        for row in grid:
            for cell in row:
                if "bbox" in cell:
                    add_normalized_bbox(cell["bbox"], page_width, page_height)

    # Process texts
    for text in doc_dict.get("texts", []):
        if "prov" in text:
            process_prov_list(text["prov"])

    # Process pictures
    for picture in doc_dict.get("pictures", []):
        if "prov" in picture:
            process_prov_list(picture["prov"])

    # Process tables
    for table in doc_dict.get("tables", []):
        # Get page number from table's prov
        page_no = 1
        if "prov" in table and table["prov"]:
            page_no = table["prov"][0].get("page_no", 1)
            process_prov_list(table["prov"])

        # Process table data
        if "data" in table:
            data = table["data"]
            if "table_cells" in data:
                process_table_cells(data["table_cells"], page_no)
            if "grid" in data:
                process_table_grid(data["grid"], page_no)

    # Process key_value_items
    for kv_item in doc_dict.get("key_value_items", []):
        if "prov" in kv_item:
            process_prov_list(kv_item["prov"])

    # Process form_items
    for form_item in doc_dict.get("form_items", []):
        if "prov" in form_item:
            process_prov_list(form_item["prov"])

    return doc_dict


def parse_output_format(format_str: Optional[str]) -> OutputFormat:
    """Parse string to OutputFormat enum.

    Args:
        format_str: String representation of output format (e.g., "json", "markdown")

    Returns:
        OutputFormat enum value, defaults to JSON if format_str is None or invalid
    """
    if not format_str:
        return OutputFormat.JSON

    format_lower = format_str.lower().strip()

    # Map common variations to OutputFormat
    format_map = {
        "json": OutputFormat.JSON,
        "yaml": OutputFormat.YAML,
        "yml": OutputFormat.YAML,
        "markdown": OutputFormat.MARKDOWN,
        "md": OutputFormat.MARKDOWN,
        "html": OutputFormat.HTML,
        "text": OutputFormat.TEXT,
        "txt": OutputFormat.TEXT,
        "plain": OutputFormat.TEXT,
        "doctags": OutputFormat.DOCTAGS,
    }

    return format_map.get(format_lower, OutputFormat.JSON)


def _build_ocr_options(ocr_config: Optional[OcrOptionsModel]) -> Tuple[bool, Any]:
    """Build OCR options based on configuration.

    Args:
        ocr_config: OCR configuration from ConversionOptions

    Returns:
        Tuple of (do_ocr, ocr_options)
    """
    # Default to no OCR if no config provided
    if ocr_config is None:
        return False, None

    # If OCR is disabled, return early
    if ocr_config.engine == OcrEngine.NONE:
        return False, None

    # Common options
    force_full_page = ocr_config.force_full_page_ocr
    bitmap_threshold = ocr_config.bitmap_area_threshold

    # Build engine-specific options
    if ocr_config.engine == OcrEngine.RAPIDOCR:
        lang = ocr_config.lang or ["english", "chinese"]
        return True, RapidOcrOptions(
            force_full_page_ocr=force_full_page,
            bitmap_area_threshold=bitmap_threshold,
            lang=lang,
        )

    elif ocr_config.engine == OcrEngine.EASYOCR:
        lang = ocr_config.lang or ["fr", "de", "es", "en"]
        return True, EasyOcrOptions(
            force_full_page_ocr=force_full_page,
            bitmap_area_threshold=bitmap_threshold,
            lang=lang,
            use_gpu=ocr_config.use_gpu,
        )

    elif ocr_config.engine == OcrEngine.TESSERACT:
        lang = ocr_config.lang or ["fra", "deu", "spa", "eng"]
        return True, TesseractOcrOptions(
            force_full_page_ocr=force_full_page,
            bitmap_area_threshold=bitmap_threshold,
            lang=lang,
            psm=ocr_config.tesseract_psm,
        )

    elif ocr_config.engine == OcrEngine.TESSERACT_CLI:
        lang = ocr_config.lang or ["fra", "deu", "spa", "eng"]
        options = TesseractCliOcrOptions(
            force_full_page_ocr=force_full_page,
            bitmap_area_threshold=bitmap_threshold,
            lang=lang,
            psm=ocr_config.tesseract_psm,
        )
        if ocr_config.tesseract_cmd:
            options.tesseract_cmd = ocr_config.tesseract_cmd
        return True, options

    elif ocr_config.engine == OcrEngine.AWS_TEXTRACT:
        region = ocr_config.aws_region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        return True, AwsTextractOcrOptions(
            force_full_page_ocr=force_full_page,
            bitmap_area_threshold=bitmap_threshold,
            region_name=region,
        )

    elif ocr_config.engine == OcrEngine.OCRMAC:
        lang = ocr_config.lang or ["fr-FR", "de-DE", "es-ES", "en-US"]
        return True, OcrMacOptions(
            force_full_page_ocr=force_full_page,
            bitmap_area_threshold=bitmap_threshold,
            lang=lang,
        )

    # Fallback to RapidOCR
    return True, RapidOcrOptions(
        force_full_page_ocr=force_full_page,
        bitmap_area_threshold=bitmap_threshold,
    )


def _create_document_converter(options: Optional[ConversionOptions] = None) -> DocumentConverter:
    """Create and configure the DocumentConverter.

    Args:
        options: Conversion options including OCR configuration

    Returns:
        Configured DocumentConverter instance
    """
    # Build OCR options
    ocr_config = options.ocr if options else None
    do_ocr, ocr_options = _build_ocr_options(ocr_config)

    # Log the OCR configuration
    if ocr_config:
        vprint(f"=== _create_document_converter: do_ocr={do_ocr}, engine={ocr_config.engine.value} ===")
        _log.info(f"Creating DocumentConverter with do_ocr={do_ocr}, ocr_engine={ocr_config.engine.value}")
    else:
        vprint(f"=== _create_document_converter: do_ocr={do_ocr}, ocr_config=None ===")
        _log.info(f"Creating DocumentConverter with do_ocr={do_ocr}, ocr_config=None (no OCR)")

    # Build table structure options
    table_mode = options.table_mode if options else TableFormerMode.FAST
    do_table_structure = options.do_table_structure if options else True
    document_timeout = options.document_timeout if options else None
    table_options = TableStructureOptions(do_cell_matching=True, mode=table_mode)

    vprint(f"=== Table config: do_table_structure={do_table_structure}, table_mode={table_mode.value}, document_timeout={document_timeout} ===")
    _log.info(f"Table config: do_table_structure={do_table_structure}, table_mode={table_mode.value}, document_timeout={document_timeout}")

    # Create pipeline options
    # IMPORTANT: PdfPipelineOptions defaults to do_ocr=True and ocr_options=OcrAutoOptions()
    # When OCR is disabled, we must explicitly set BOTH do_ocr=False AND ocr_options to
    # prevent the OCR model from being instantiated during pipeline initialization.
    pipeline_kwargs = {
        "do_table_structure": do_table_structure,
        "table_structure_options": table_options,
    }
    if document_timeout is not None:
        pipeline_kwargs["document_timeout"] = document_timeout

    if do_ocr and ocr_options:
        # OCR enabled with specific options
        vprint(f"=== Creating PdfPipelineOptions with do_ocr=True, ocr_options={type(ocr_options).__name__}, table_mode={table_mode.value}, do_table_structure={do_table_structure} ===")
        pdf_pipeline_options = PdfPipelineOptions(
            do_ocr=True,
            ocr_options=ocr_options,
            **pipeline_kwargs,
        )
        _log.info(f"OCR enabled with options: {type(ocr_options).__name__}")
    else:
        # OCR disabled - explicitly set ocr_options to None to prevent model initialization
        vprint(f"=== Creating PdfPipelineOptions with do_ocr=False, table_mode={table_mode.value}, do_table_structure={do_table_structure} ===")
        pdf_pipeline_options = PdfPipelineOptions(
            do_ocr=False,
            **pipeline_kwargs,
        )
        _log.info("OCR disabled (do_ocr=False)")

    # Select pipeline implementation (threaded import is lazy to avoid slowing Lambda init)
    pipeline_mode = options.pipeline if options else PipelineMode.LEGACY
    if pipeline_mode == PipelineMode.THREADED:
        from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
        pipeline_cls = StandardPdfPipeline
    else:
        pipeline_cls = LegacyStandardPdfPipeline

    # Log the final pipeline options for debugging
    vprint(f"=== PdfPipelineOptions created: do_ocr={pdf_pipeline_options.do_ocr}, pipeline={pipeline_mode.value} ===")
    _log.info(f"PdfPipelineOptions: do_ocr={pdf_pipeline_options.do_ocr}, ocr_options type={type(pdf_pipeline_options.ocr_options).__name__}, pipeline={pipeline_mode.value}")

    return DocumentConverter(
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
        ],
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=pipeline_cls,
                backend=PyPdfiumDocumentBackend,
                pipeline_options=pdf_pipeline_options,
            ),
            InputFormat.IMAGE: ImageFormatOption(
                pipeline_options=pdf_pipeline_options,
            ),
            InputFormat.DOCX: WordFormatOption(pipeline_cls=SimplePipeline),
        },
    )


def convert_files(
    file_paths: List[Path],
    options: Optional[ConversionOptions] = None,
) -> List[DoclingConversionResult]:
    """Convert documents using Docling.

    Returns raw Docling conversion results, allowing handlers to process
    the documents before formatting for output.

    Args:
        file_paths: List of paths to files to convert
        options: Optional conversion options including OCR configuration

    Returns:
        List of DoclingConversionResult objects containing raw Docling documents
    """
    if not file_paths:
        return []

    # Log the conversion options for debugging
    if options:
        ocr_desc = f"OCR engine={options.ocr.engine.value}" if options.ocr else "OCR disabled"
        vprint(f"=== convert_files: {ocr_desc}, do_table_structure={options.do_table_structure}, table_mode={options.table_mode.value}, document_timeout={options.document_timeout} ===")
        _log.info(f"convert_files called with {ocr_desc}, do_table_structure={options.do_table_structure}, table_mode={options.table_mode.value}, document_timeout={options.document_timeout}")
    else:
        vprint("=== convert_files: no options (defaults) ===")
        _log.info("convert_files called with no options (using defaults)")

    # Initialize DocumentConverter with options
    with vtimer("DocumentConverter.__init__"):
        doc_converter = _create_document_converter(options)

    # Convert all files (convert_all returns an iterator — real work happens during iteration)
    results = []
    with vtimer(f"doc_converter.convert_all ({len(file_paths)} file(s))"):
        conv_results = doc_converter.convert_all(file_paths)

        for res in conv_results:
            # Extract filename from input
            filename = "unknown"
            if hasattr(res.input, "file") and res.input.file:
                # file is a PurePath, so we can get the name
                filename = (
                    str(res.input.file.name) if res.input.file.name else str(res.input.file)
                )

            # Extract relevant information from ConversionResult
            status_value = (
                res.status.value if hasattr(res.status, "value") else str(res.status)
            )

            # Build errors list
            errors = [
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
            ]

            # Extract confidence report if available
            confidence_data = None
            if hasattr(res, "confidence") and res.confidence:
                try:
                    # Convert ConfidenceReport to dict for serialization
                    if hasattr(res.confidence, "model_dump"):
                        confidence_data = res.confidence.model_dump(mode="json")
                    elif hasattr(res.confidence, "dict"):
                        confidence_data = res.confidence.dict()
                except Exception as e:
                    _log.warning(f"Error extracting confidence for {filename}: {e}")

            results.append(
                DoclingConversionResult(
                    filename=filename,
                    status=status_value,
                    document=res.document if status_value == "success" else None,
                    errors=errors,
                    confidence=confidence_data,
                )
            )

            vprint(f"=== convert_files: {filename} status={status_value} ===")

    return results


def format_document(
    document: Any,
    output_format: OutputFormat,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Format a Docling document to the requested output format.

    Args:
        document: Raw Docling document object
        output_format: The desired output format

    Returns:
        Tuple of (output_content, document_dict)
        - For JSON: (None, normalized_doc_dict)
        - For other formats: (formatted_content, normalized_doc_dict)
    """
    # Always get the document dict with normalized bboxes
    doc_dict = document.export_to_dict()
    normalized_doc_dict = normalize_bboxes(doc_dict)

    if output_format == OutputFormat.JSON:
        return None, normalized_doc_dict

    # For non-JSON formats, generate the output content
    if output_format == OutputFormat.MARKDOWN:
        output_content = document.export_to_markdown()
    elif output_format == OutputFormat.YAML:
        # save_as_yaml writes to a file, so we use a temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            document.save_as_yaml(filename=tmp_path, image_mode=ImageRefMode.PLACEHOLDER)
            output_content = tmp_path.read_text(encoding="utf-8")
            tmp_path.unlink()  # Clean up
    elif output_format == OutputFormat.HTML:
        # save_as_html writes to a file, so we use a temporary file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".html", delete=False
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            document.save_as_html(filename=tmp_path, image_mode=ImageRefMode.PLACEHOLDER)
            output_content = tmp_path.read_text(encoding="utf-8")
            tmp_path.unlink()  # Clean up
    elif output_format == OutputFormat.TEXT:
        output_content = document.export_to_markdown(strict_text=True)
    elif output_format == OutputFormat.DOCTAGS:
        output_content = document.export_to_doctags()
    else:
        # Fallback to markdown
        output_content = document.export_to_markdown()

    return output_content, normalized_doc_dict


def format_conversion_result(
    result: DoclingConversionResult,
    output_format: OutputFormat,
) -> ConversionResultResponse:
    """Format a single conversion result for response.

    Args:
        result: Raw conversion result from convert_files()
        output_format: The desired output format

    Returns:
        ConversionResultResponse ready for serialization
    """
    result_data: Dict[str, Any] = {
        "filename": result.filename,
        "status": result.status,
        "errors": [ErrorResponse(**e) for e in result.errors],
    }

    # Include confidence if available
    if result.confidence:
        result_data["confidence"] = result.confidence

    if result.document and result.status == "success":
        try:
            output_content, doc_dict = format_document(result.document, output_format)
            result_data["document"] = doc_dict
            result_data["output"] = output_content
            result_data["output_format"] = output_format.value
        except Exception as e:
            _log.error(f"Error formatting document for {result.filename}: {e}")
            result_data["errors"].append(
                ErrorResponse(
                    component_type="api",
                    module_name="common",
                    error_message=f"Error formatting document: {str(e)}",
                )
            )

    return ConversionResultResponse(**result_data)


def format_results_for_output(
    results: List[ConversionResultResponse],
    output_format: OutputFormat,
) -> Tuple[str, str]:
    """Format results for non-JSON output.

    Args:
        results: List of conversion results
        output_format: The output format to use

    Returns:
        Tuple of (content_string, media_type)
    """
    if not results:
        return "", "text/plain"

    output_parts = []
    for result in results:
        if result.status != "success":
            # If conversion failed, include error info
            error_msg = f"Error converting {result.filename}: {', '.join([e.error_message for e in result.errors])}"
            if output_format == OutputFormat.MARKDOWN:
                output_parts.append(f"# Error: {error_msg}\n")
            elif output_format == OutputFormat.HTML:
                output_parts.append(
                    f"<h1>Error: {result.filename}</h1><p>{error_msg}</p>\n"
                )
            else:
                output_parts.append(f"Error: {error_msg}\n")
        else:
            # Get the output content
            output_content = result.output
            if not output_content:
                output_content = "(No output available)"

            if output_format == OutputFormat.MARKDOWN:
                output_parts.append(f"# {result.filename}\n\n{output_content}\n")
            elif output_format == OutputFormat.YAML:
                output_parts.append(f"# {result.filename}\n{output_content}\n")
            elif output_format == OutputFormat.HTML:
                output_parts.append(f"<h1>{result.filename}</h1>\n{output_content}\n")
            elif output_format == OutputFormat.TEXT:
                output_parts.append(
                    f"{result.filename}\n{'=' * len(result.filename)}\n\n{output_content}\n\n"
                )
            elif output_format == OutputFormat.DOCTAGS:
                output_parts.append(f"# {result.filename}\n{output_content}\n")

    # Join all parts with separator
    if len(output_parts) > 1:
        separator = "\n" + ("=" * 80) + "\n\n"
        content = separator.join(output_parts)
    else:
        content = output_parts[0] if output_parts else ""

    # Determine media type
    media_type_map = {
        OutputFormat.MARKDOWN: "text/markdown",
        OutputFormat.YAML: "application/x-yaml",
        OutputFormat.HTML: "text/html",
        OutputFormat.TEXT: "text/plain",
        OutputFormat.DOCTAGS: "text/plain",
        OutputFormat.JSON: "application/json",
    }

    return content, media_type_map.get(output_format, "text/plain")
