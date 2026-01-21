"""AWS Lambda handler for Docling document conversion.

This handler provides document conversion functionality via AWS Lambda,
downloading files from S3 and using the same conversion logic as the HTTP server.
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent directory to path to use local modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import boto3
from botocore.exceptions import ClientError

from rest.common import (
    MAX_FILE_SIZE,
    MAX_FILES,
    ConversionOptions,
    ConversionResultResponse,
    DoclingConversionResult,
    OcrOptionsModel,
    OutputFormat,
    convert_files,
    format_document,
    format_conversion_result,
    format_results_for_output,
    normalize_bboxes,
    parse_output_format,
)

# Configure logging - set to DEBUG for detailed output
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
_log = logging.getLogger(__name__)

# Also configure the rest.common logger
logging.getLogger('rest.common').setLevel(logging.DEBUG)

print("=== Lambda handler module loaded ===")

# File extension mapping for output formats
FORMAT_EXTENSIONS = {
    OutputFormat.JSON: ".json",
    OutputFormat.YAML: ".yaml",
    OutputFormat.MARKDOWN: ".md",
    OutputFormat.HTML: ".html",
    OutputFormat.TEXT: ".txt",
    OutputFormat.DOCTAGS: ".doctags",
}

# Content type mapping for output formats
FORMAT_CONTENT_TYPES = {
    OutputFormat.JSON: "application/json",
    OutputFormat.YAML: "application/x-yaml",
    OutputFormat.MARKDOWN: "text/markdown",
    OutputFormat.HTML: "text/html",
    OutputFormat.TEXT: "text/plain",
    OutputFormat.DOCTAGS: "text/plain",
}


class LambdaError(Exception):
    """Custom exception for Lambda handler errors."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _download_s3_file(
    s3_client: Any,
    region: str,
    bucket: str,
    key: str,
    temp_dir: Path,
) -> Path:
    """Download a file from S3 to a temporary directory.

    Args:
        s3_client: Boto3 S3 client
        region: AWS region for the bucket
        bucket: S3 bucket name
        key: S3 object key
        temp_dir: Temporary directory to save the file

    Returns:
        Path to the downloaded file

    Raises:
        LambdaError: If download fails or file is too large
    """
    # Extract filename from key
    filename = Path(key).name
    if not filename:
        raise LambdaError(f"Invalid S3 key: {key}")

    local_path = temp_dir / filename

    try:
        # Check file size before downloading
        head_response = s3_client.head_object(Bucket=bucket, Key=key)
        file_size = head_response.get("ContentLength", 0)

        if file_size > MAX_FILE_SIZE:
            raise LambdaError(
                f"File '{key}' exceeds maximum size of "
                f"{MAX_FILE_SIZE / (1024 * 1024):.0f}MB. "
                f"File size: {file_size / (1024 * 1024):.2f}MB",
                status_code=400,
            )

        # Download the file
        s3_client.download_file(bucket, key, str(local_path))
        _log.info(f"Downloaded {bucket}/{key} to {local_path} ({file_size} bytes)")

        return local_path

    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "404" or error_code == "NoSuchKey":
            raise LambdaError(f"File not found: s3://{bucket}/{key}", status_code=404)
        elif error_code == "403" or error_code == "AccessDenied":
            raise LambdaError(
                f"Access denied to s3://{bucket}/{key}", status_code=403
            )
        else:
            raise LambdaError(
                f"Error downloading s3://{bucket}/{key}: {str(e)}", status_code=500
            )


def _parse_request(
    event: Dict[str, Any]
) -> tuple[List[Dict[str, str]], OutputFormat, List[OutputFormat], ConversionOptions]:
    """Parse and validate the Lambda request.

    Args:
        event: Lambda event dictionary

    Returns:
        Tuple of (files_list, output_format, additional_formats, conversion_options)

    Raises:
        LambdaError: If request is invalid
    """
    # Handle API Gateway proxy integration
    body = event
    if "body" in event:
        body_str = event.get("body", "{}")
        if isinstance(body_str, str):
            try:
                body = json.loads(body_str) if body_str else {}
            except json.JSONDecodeError as e:
                raise LambdaError(f"Invalid JSON in request body: {str(e)}")
        else:
            body = body_str or {}

    # Extract files
    files = body.get("files", [])
    if not files:
        raise LambdaError("At least one file must be provided in 'files' array")

    if not isinstance(files, list):
        raise LambdaError("'files' must be an array")

    if len(files) > MAX_FILES:
        raise LambdaError(
            f"Too many files. Maximum {MAX_FILES} files allowed per request."
        )

    # Validate each file entry
    for i, file_entry in enumerate(files):
        if not isinstance(file_entry, dict):
            raise LambdaError(f"File entry {i} must be an object")
        if "bucket" not in file_entry:
            raise LambdaError(f"File entry {i} missing required 'bucket' field")
        if "key" not in file_entry:
            raise LambdaError(f"File entry {i} missing required 'key' field")

    # Parse output format
    output_format_str = body.get("output_format", "json")
    output_format = parse_output_format(output_format_str)

    # Parse additional formats to save to S3
    additional_formats_raw = body.get("additional_formats", [])
    if not isinstance(additional_formats_raw, list):
        raise LambdaError("'additional_formats' must be an array")

    additional_formats: List[OutputFormat] = []
    for fmt_str in additional_formats_raw:
        if not isinstance(fmt_str, str):
            raise LambdaError(f"Invalid format in 'additional_formats': {fmt_str}")
        fmt = parse_output_format(fmt_str)
        if fmt not in additional_formats:
            additional_formats.append(fmt)

    # Parse options
    options_dict = body.get("options", {})
    _log.info(f"Parsing options: {options_dict}")
    try:
        conversion_options = ConversionOptions(**options_dict)
        _log.info(f"ConversionOptions created: ocr={conversion_options.ocr}")
        if conversion_options.ocr:
            _log.info(f"OCR config: engine={conversion_options.ocr.engine.value}, force_full_page={conversion_options.ocr.force_full_page_ocr}")
    except Exception as e:
        raise LambdaError(f"Invalid options format: {str(e)}")

    return files, output_format, additional_formats, conversion_options


def _save_format_to_s3(
    s3_client: Any,
    bucket: str,
    key: str,
    document: Any,
    output_format: OutputFormat,
) -> str:
    """Save a document in the specified format to S3.

    Args:
        s3_client: Boto3 S3 client
        bucket: S3 bucket name
        key: Original S3 key (base for the new key)
        document: Raw Docling document object
        output_format: The format to save

    Returns:
        The S3 key where the file was saved
    """
    extension = FORMAT_EXTENSIONS.get(output_format, ".txt")
    content_type = FORMAT_CONTENT_TYPES.get(output_format, "text/plain")
    output_key = f"{key}.enriched{extension}"

    # Generate the content
    output_content, doc_dict = format_document(document, output_format)

    if output_format == OutputFormat.JSON:
        # For JSON, we save the normalized document dict
        body = json.dumps(doc_dict, indent=2)
    else:
        # For other formats, use the formatted output
        body = output_content or ""

    s3_client.put_object(
        Bucket=bucket,
        Key=output_key,
        Body=body.encode("utf-8"),
        ContentType=content_type,
    )

    _log.info(f"Saved {output_format.value} to s3://{bucket}/{output_key}")
    return output_key


def _process_converted_document(
    result: DoclingConversionResult,
    s3_client: Any,
    bucket: str,
    key: str,
    additional_formats: List[OutputFormat],
) -> Dict[str, str]:
    """Process a converted document and save additional formats to S3.

    Args:
        result: The raw conversion result containing the Docling document
        s3_client: Boto3 S3 client
        bucket: Original S3 bucket
        key: Original S3 key
        additional_formats: List of formats to save to S3

    Returns:
        Dictionary mapping format names to S3 keys where they were saved
    """
    saved_files: Dict[str, str] = {}

    if result.status != "success" or result.document is None:
        return saved_files

    # Save each additional format to S3
    for fmt in additional_formats:
        try:
            output_key = _save_format_to_s3(
                s3_client, bucket, key, result.document, fmt
            )
            saved_files[fmt.value] = f"s3://{bucket}/{output_key}"
        except Exception as e:
            _log.error(f"Error saving {fmt.value} for {result.filename}: {e}")

    _log.info(f"Processed document: {result.filename}, saved {len(saved_files)} additional format(s)")
    return saved_files


def _build_response(
    results: List[ConversionResultResponse],
    output_format: OutputFormat,
    saved_files_map: Optional[Dict[str, Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Build the Lambda response.

    Args:
        results: List of conversion results
        output_format: The output format used
        saved_files_map: Optional mapping of filename to saved S3 locations

    Returns:
        Lambda response dictionary
    """
    if output_format == OutputFormat.JSON:
        # Return JSON array of results, optionally with saved_files info
        response_data = []
        for r in results:
            result_dict = r.model_dump()
            # Add saved_files if available
            if saved_files_map and r.filename in saved_files_map:
                result_dict["saved_files"] = saved_files_map[r.filename]
            response_data.append(result_dict)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(response_data),
        }
    else:
        # Return formatted content
        content, media_type = format_results_for_output(results, output_format)
        return {
            "statusCode": 200,
            "headers": {"Content-Type": media_type},
            "body": content,
        }


def _build_error_response(error: LambdaError) -> Dict[str, Any]:
    """Build an error response.

    Args:
        error: The LambdaError exception

    Returns:
        Lambda error response dictionary
    """
    return {
        "statusCode": error.status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": error.message}),
    }


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """AWS Lambda handler for document conversion.

    Request format:
    {
        "files": [
            {
                "region": "us-east-1",  // optional, defaults to Lambda region
                "bucket": "my-bucket",
                "key": "documents/file.pdf"
            }
        ],
        "output_format": "json",  // optional, defaults to "json"
        "additional_formats": ["markdown", "html"],  // optional, formats to save to S3
        "options": {}  // optional, reserved for future use
    }

    Response format:
    - For JSON: Array of ConversionResultResponse objects with optional saved_files
    - For other formats: Raw content string with appropriate content-type header

    The additional_formats will be saved back to the same S3 bucket with the
    original key plus the appropriate extension (e.g., "doc.pdf" -> "doc.pdf.md").

    Args:
        event: Lambda event dictionary
        context: Lambda context object

    Returns:
        Lambda response dictionary with statusCode, headers, and body
    """
    print("=== Lambda handler function called ===")
    _log.info(f"Lambda handler invoked with event keys: {list(event.keys())}")

    try:
        # Parse request
        files, output_format, additional_formats, conversion_options = _parse_request(event)

        # Log OCR configuration explicitly
        print(f"=== OCR Config: {conversion_options.ocr} ===")
        if conversion_options.ocr:
            print(f"=== OCR Engine: {conversion_options.ocr.engine.value} ===")
        else:
            print("=== OCR is DISABLED (no OCR options specified) ===")

        _log.info(
            f"Processing {len(files)} file(s) with output_format={output_format.value}, "
            f"additional_formats={[f.value for f in additional_formats]}"
        )

        # Get default region from environment or Lambda context
        default_region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or "us-east-1"
        )

        # Create S3 client cache by region
        s3_clients: Dict[str, Any] = {}

        def get_s3_client(region: str) -> Any:
            if region not in s3_clients:
                s3_clients[region] = boto3.client("s3", region_name=region)
            return s3_clients[region]

        # Track file metadata for post-processing
        file_metadata: List[Dict[str, str]] = []

        # Track saved files for response
        saved_files_map: Dict[str, Dict[str, str]] = {}

        # Download files and convert
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            file_paths = []

            for file_entry in files:
                region = file_entry.get("region", default_region)
                bucket = file_entry["bucket"]
                key = file_entry["key"]

                s3_client = get_s3_client(region)
                local_path = _download_s3_file(
                    s3_client, region, bucket, key, temp_path
                )
                file_paths.append(local_path)
                file_metadata.append({
                    "region": region,
                    "bucket": bucket,
                    "key": key,
                })

            if not file_paths:
                raise LambdaError("No valid files to convert")

            # Convert files using common module (returns raw Docling results)
            raw_results = convert_files(file_paths, conversion_options)

            # Process each converted document and save additional formats
            for i, result in enumerate(raw_results):
                if i < len(file_metadata):
                    metadata = file_metadata[i]
                    s3_client = get_s3_client(metadata["region"])
                    saved_files = _process_converted_document(
                        result,
                        s3_client,
                        metadata["bucket"],
                        metadata["key"],
                        additional_formats,
                    )
                    if saved_files:
                        saved_files_map[result.filename] = saved_files

            # Format results for output response
            results = [
                format_conversion_result(r, output_format) for r in raw_results
            ]

        # Build response
        return _build_response(results, output_format, saved_files_map)

    except LambdaError as e:
        _log.error(f"Lambda error: {e.message}")
        return _build_error_response(e)
    except Exception as e:
        _log.error(f"Unexpected error: {str(e)}", exc_info=True)
        return _build_error_response(
            LambdaError(f"Internal error: {str(e)}", status_code=500)
        )
