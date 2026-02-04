#!/bin/zsh

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Docling: Invoke Document Enrichment Lambda
# @raycast.mode fullOutput

# Optional parameters:
# @raycast.icon 🤖
# @raycast.argument1 { "type": "text", "placeholder": "key"}
# @raycast.argument2 { "type": "text", "placeholder": "bucket" , "optional": true}
# @raycast.argument3 { "type": "text", "placeholder": "output_format" , "optional": true}
# @raycast.argument4 { "type": "text", "placeholder": "additional_formats" , "optional": true}
# @raycast.argument5 { "type": "text", "placeholder": "ocr_engine" , "optional": true}
# @raycast.argument6 { "type": "text", "placeholder": "ocr_lang" , "optional": true}
# @raycast.argument7 { "type": "text", "placeholder": "ocr_force_full_page_ocr" , "optional": true}
# @raycast.argument8 { "type": "text", "placeholder": "ocr_bitmap_area_threshold" , "optional": true}
# @raycast.packageName Docling

# Documentation:
# @raycast.description Invoke the Document Enrichment Lambda.
# @raycast.author Jason Douglas
echo "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# make sure we are in this directory before we run
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."

# make sure we are using the latest env vars.
source .envrc


KEY=$1
BUCKET=$2
OUTPUT_FORMAT=$3
ADDITIONAL_FORMATS=$4
OCR_ENGINE=$5
OCR_LANG=$6
OCR_FORCE_FULL_PAGE_OCR=$7
OCR_BITMAP_AREA_THRESHOLD=$8

# FORCE_CONVERSION can be set via environment variable (e.g., FORCE_CONVERSION=true ./lambda-invoke.sh ...)
if [ -z "$FORCE_CONVERSION" ]; then
    FORCE_CONVERSION="false"
fi

if [ -z "$BUCKET" ]; then
    BUCKET="heniff-ai-document-engine"
fi
if [ -z "$OUTPUT_FORMAT" ]; then
    OUTPUT_FORMAT="json"
fi
if [ -z "$ADDITIONAL_FORMATS" ]; then
    ADDITIONAL_FORMATS="markdown"
fi

if [ -z "$OCR_ENGINE" ]; then
    echo "Invoking Document Enrichment Lambda for $BUCKET/$KEY with output format $OUTPUT_FORMAT..."
    aws lambda invoke \
        --function-name icg-document-enrichment \
        --payload "$(jq -n \
            --arg bucket "$BUCKET" \
            --arg key "$KEY" \
            --arg output_format "$OUTPUT_FORMAT" \
            --arg additional_formats "$ADDITIONAL_FORMATS" \
            --argjson force_conversion "$FORCE_CONVERSION" \
            '{files: [{bucket: $bucket, key: $key}], output_format: $output_format, additional_formats: ($additional_formats | split(",") | map(gsub("^\\s+|\\s+$"; ""))), force_conversion: $force_conversion, options: {}}')" \
        --cli-binary-format raw-in-base64-out \
        /dev/stdout
else
    if [ -z "$OCR_LANG" ]; then
        OCR_LANG="english"
    fi
    if [ -z "$OCR_FORCE_FULL_PAGE_OCR" ]; then
        OCR_FORCE_FULL_PAGE_OCR="false"
    fi
    if [ -z "$OCR_BITMAP_AREA_THRESHOLD" ]; then
        OCR_BITMAP_AREA_THRESHOLD="0.05"
    fi

    echo "Invoking Document Enrichment Lambda with OCR for $BUCKET/$KEY with output format $OUTPUT_FORMAT..."
    aws lambda invoke \
        --function-name icg-document-enrichment \
        --payload "$(jq -n \
            --arg bucket "$BUCKET" \
            --arg key "$KEY" \
            --arg output_format "$OUTPUT_FORMAT" \
            --arg additional_formats "$ADDITIONAL_FORMATS" \
            --argjson force_conversion "$FORCE_CONVERSION" \
            --arg ocr_engine "$OCR_ENGINE" \
            --arg ocr_lang "$OCR_LANG" \
            --argjson ocr_force_full_page_ocr "$OCR_FORCE_FULL_PAGE_OCR" \
            --argjson ocr_bitmap_area_threshold "$OCR_BITMAP_AREA_THRESHOLD" \
            '{
                files: [{bucket: $bucket, key: $key}],
                output_format: $output_format,
                additional_formats: ($additional_formats | split(",") | map(gsub("^\\s+|\\s+$"; ""))),
                force_conversion: $force_conversion,
                options: {
                    ocr: {
                        engine: $ocr_engine,
                        lang: ($ocr_lang | split(",") | map(gsub("^\\s+|\\s+$"; ""))),
                        force_full_page_ocr: $ocr_force_full_page_ocr,
                        bitmap_area_threshold: $ocr_bitmap_area_threshold
                    }
                }
            }')" \
        --cli-binary-format raw-in-base64-out \
        /dev/stdout
fi


# e
# if [ -z "$OCR_LANG" ]; then
#     OCR_LANG=["english", "chinese"]
# fi

# Sync the storage key to the dev environment