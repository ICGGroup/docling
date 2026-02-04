import io
import logging
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Optional, Type

import numpy
from docling_core.types.doc import BoundingBox, CoordOrigin
from docling_core.types.doc.page import BoundingRectangle, TextCell

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import (
    AwsTextractOcrOptions,
    OcrOptions,
)
from docling.datamodel.settings import settings
from docling.models.base_ocr_model import BaseOcrModel
from docling.utils.profiling import TimeRecorder

_log = logging.getLogger(__name__)


class AwsTextractOcrModel(BaseOcrModel):
    def __init__(
        self,
        enabled: bool,
        artifacts_path: Optional[Path],
        options: AwsTextractOcrOptions,
        accelerator_options: AcceleratorOptions,
    ):
        super().__init__(
            enabled=enabled,
            artifacts_path=artifacts_path,
            options=options,
            accelerator_options=accelerator_options,
        )
        self.options: AwsTextractOcrOptions

        self.scale = 3  # multiplier for 72 dpi == 216 dpi.

        if self.enabled:
            try:
                import boto3
                from botocore.exceptions import BotoCoreError, ClientError
            except ImportError:
                raise ImportError(
                    "boto3 is not installed. Please install it via `pip install boto3` to use AWS Textract OCR engine. "
                    "Alternatively, Docling has support for other OCR engines. See the documentation."
                )

            # Initialize the Textract client
            # Supports multiple authentication methods:
            # 1. Explicit credentials via options (aws_access_key_id, aws_secret_access_key, etc.)
            # 2. Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
            # 3. AWS IAM role (when running in Lambda, EC2, ECS, etc.)
            # 4. AWS credential file (~/.aws/credentials)
            # 5. AWS config file (~/.aws/config)
            #
            # When no explicit credentials are provided, boto3 automatically uses the
            # default credential chain, which includes IAM role credentials in AWS environments.
            try:
                client_kwargs = {}

                # Determine region: explicit option > AWS_REGION env > AWS_DEFAULT_REGION env
                region = self.options.region_name
                if not region:
                    import os
                    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
                if region:
                    client_kwargs["region_name"] = region

                # Check if explicit credentials are provided in options
                has_explicit_creds = bool(
                    self.options.aws_access_key_id and self.options.aws_secret_access_key
                )

                if has_explicit_creds:
                    # Use explicitly provided credentials
                    client_kwargs["aws_access_key_id"] = self.options.aws_access_key_id
                    client_kwargs["aws_secret_access_key"] = self.options.aws_secret_access_key
                    if self.options.aws_session_token:
                        client_kwargs["aws_session_token"] = self.options.aws_session_token
                    _log.debug("Using explicit AWS credentials from options")
                else:
                    # Let boto3 use the default credential chain
                    # This automatically handles:
                    # - Environment variables (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
                    # - IAM role credentials (Lambda execution role, EC2 instance role, etc.)
                    # - AWS credential/config files
                    _log.debug("Using boto3 default credential chain (env vars, IAM role, or config files)")

                if self.options.endpoint_url:
                    client_kwargs["endpoint_url"] = self.options.endpoint_url

                self.client = boto3.client("textract", **client_kwargs)

                # Log the credential source for debugging
                session = boto3.Session()
                credentials = session.get_credentials()
                if credentials:
                    cred_method = getattr(credentials, 'method', 'unknown')
                    _log.info(f"AWS Textract client initialized using credential method: {cred_method}")
                else:
                    _log.info("AWS Textract client initialized (credential method: default chain)")

                _log.info(f"AWS OCR engine initialized, region={region or 'default'}")
            except (BotoCoreError, ClientError) as e:
                raise RuntimeError(
                    f"Failed to initialize AWS Textract client: {e}. "
                    "Please ensure AWS credentials are configured correctly. "
                    "When running locally, set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables, "
                    "or configure ~/.aws/credentials. "
                    "When running in AWS Lambda/EC2/ECS, ensure the execution role has textract:DetectDocumentText permission. "
                    "See https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html"
                ) from e

    def _image_to_bytes(self, image) -> bytes:
        """Convert PIL Image to bytes in PNG format."""
        img_bytes = io.BytesIO()
        image.save(img_bytes, format="PNG")
        return img_bytes.getvalue()

    def _parse_textract_response(self, response: dict, ocr_rect, im_width: int, im_height: int) -> list[TextCell]:
        """Parse AWS Textract response and convert to TextCell format."""
        cells = []
        blocks = response.get("Blocks", [])
        
        # Process only LINE blocks (Textract returns WORD, LINE, PAGE, etc.)
        line_blocks = [block for block in blocks if block.get("BlockType") == "LINE"]
        
        for ix, line_block in enumerate(line_blocks):
            text = line_block.get("Text", "")
            confidence = line_block.get("Confidence", 0.0) / 100.0  # Convert from 0-100 to 0-1
            
            # Get bounding box from Geometry
            geometry = line_block.get("Geometry", {})
            bounding_box = geometry.get("BoundingBox", {})
            
            # Textract returns normalized coordinates (0-1)
            left_norm = bounding_box.get("Left", 0.0)
            top_norm = bounding_box.get("Top", 0.0)
            width_norm = bounding_box.get("Width", 0.0)
            height_norm = bounding_box.get("Height", 0.0)
            
            # Convert to pixel coordinates relative to the cropped image
            x1 = left_norm * im_width
            y1 = top_norm * im_height
            x2 = (left_norm + width_norm) * im_width
            y2 = (top_norm + height_norm) * im_height
            
            # Convert to page coordinates (accounting for scale and crop offset)
            left = (x1 / self.scale) + ocr_rect.l
            top = (y1 / self.scale) + ocr_rect.t
            right = (x2 / self.scale) + ocr_rect.l
            bottom = (y2 / self.scale) + ocr_rect.t
            
            cells.append(
                TextCell(
                    index=ix,
                    text=text,
                    orig=text,
                    from_ocr=True,
                    confidence=confidence,
                    rect=BoundingRectangle.from_bounding_box(
                        BoundingBox.from_tuple(
                            coord=(left, top, right, bottom),
                            origin=CoordOrigin.TOPLEFT,
                        )
                    ),
                )
            )
        
        return cells

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:
        if not self.enabled:
            yield from page_batch
            return

        for page in page_batch:
            assert page._backend is not None
            if not page._backend.is_valid():
                yield page
            else:
                with TimeRecorder(conv_res, "ocr"):
                    ocr_rects = self.get_ocr_rects(page)

                    all_ocr_cells = []
                    for ocr_rect in ocr_rects:
                        # Skip zero area boxes
                        if ocr_rect.area() == 0:
                            continue
                        high_res_image = page._backend.get_page_image(
                            scale=self.scale, cropbox=ocr_rect
                        )
                        im = numpy.array(high_res_image)
                        im_width, im_height = high_res_image.size

                        # Convert image to bytes
                        image_bytes = self._image_to_bytes(high_res_image)

                        try:
                            # Call AWS Textract
                            image_size_kb = len(image_bytes) / 1024
                            _log.info(
                                f"AWS Textract request: page={page.page_no}, "
                                f"rect={ocr_rect}, image_size={image_size_kb:.1f}KB, "
                                f"dimensions={im_width}x{im_height}"
                            )

                            start_time = time.time()
                            response = self.client.detect_document_text(
                                Document={"Bytes": image_bytes}
                            )
                            elapsed_ms = (time.time() - start_time) * 1000

                            # Extract response metadata
                            response_metadata = response.get("ResponseMetadata", {})
                            request_id = response_metadata.get("RequestId", "N/A")
                            http_status = response_metadata.get("HTTPStatusCode", "N/A")
                            blocks = response.get("Blocks", [])
                            num_blocks = len(blocks)
                            num_lines = len([b for b in blocks if b.get("BlockType") == "LINE"])
                            num_words = len([b for b in blocks if b.get("BlockType") == "WORD"])

                            _log.info(
                                f"AWS Textract response: page={page.page_no}, "
                                f"request_id={request_id}, status={http_status}, "
                                f"elapsed={elapsed_ms:.1f}ms, blocks={num_blocks}, "
                                f"lines={num_lines}, words={num_words}"
                            )

                            # Parse response and convert to TextCell format
                            cells = self._parse_textract_response(
                                response, ocr_rect, im_width, im_height
                            )

                            # Filter by confidence threshold if specified
                            if self.options.confidence_threshold is not None:
                                original_count = len(cells)
                                cells = [
                                    cell
                                    for cell in cells
                                    if cell.confidence >= self.options.confidence_threshold
                                ]
                                if original_count != len(cells):
                                    _log.info(
                                        f"AWS Textract: filtered {original_count - len(cells)} cells "
                                        f"below confidence threshold {self.options.confidence_threshold}"
                                    )

                            all_ocr_cells.extend(cells)

                        except Exception as e:
                            _log.warning(
                                f"AWS Textract failed for page {page.page_no}, rect {ocr_rect}: {e}"
                            )
                            # Continue processing other rects even if one fails

                        del high_res_image
                        del im

                    # Post-process the cells
                    self.post_process_cells(all_ocr_cells, page)

                # DEBUG code:
                if settings.debug.visualize_ocr:
                    self.draw_ocr_rects_and_cells(conv_res, page, ocr_rects)

                yield page

    @classmethod
    def get_options_type(cls) -> Type[OcrOptions]:
        return AwsTextractOcrOptions
