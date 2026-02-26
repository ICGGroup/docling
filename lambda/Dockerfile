FROM public.ecr.aws/lambda/python:3.12

# Install system dependencies required by Docling
RUN dnf install -y mesa-libGL glib2 && \
    dnf clean all

# This will install torch with *only* cpu support
# Remove the --extra-index-url part if you want to install all the gpu requirements
# For more details in the different torch distribution visit https://pytorch.org/.
RUN pip install --no-cache-dir docling --extra-index-url https://download.pytorch.org/whl/cpu

# Install additional dependencies for Lambda handler
RUN pip install --no-cache-dir boto3 rapidocr onnxruntime

# Download Docling models to a known stable path baked into the image.
# Without DOCLING_ARTIFACTS_PATH, each model class calls download_models()
# at runtime, which checks HuggingFace Hub — wasting Lambda cold-start time
# or failing without internet access.
RUN docling-tools models download -o /opt/docling/models

# Point the pipeline at pre-downloaded models (skips all runtime downloads)
ENV DOCLING_ARTIFACTS_PATH=/opt/docling/models

# HF/Torch cache dirs for any residual caching
ENV HF_HOME=/tmp/
ENV TORCH_HOME=/tmp/

# Copy application code (our fork overrides the pip-installed docling package
# because Lambda runtime inserts LAMBDA_TASK_ROOT at sys.path[0])
COPY rest ${LAMBDA_TASK_ROOT}/rest
COPY lambda/handler.py ${LAMBDA_TASK_ROOT}/lambda/handler.py
COPY docling ${LAMBDA_TASK_ROOT}/docling

# Purge stale .pyc from local dev, then precompile for this container's Python.
# This avoids runtime compilation overhead during Lambda init.
RUN find ${LAMBDA_TASK_ROOT} -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true && \
    python -m compileall -q -q ${LAMBDA_TASK_ROOT}/docling ${LAMBDA_TASK_ROOT}/rest ${LAMBDA_TASK_ROOT}/lambda

# On container environments, always set a thread budget to avoid undesired thread congestion.
ENV OMP_NUM_THREADS=4

# Ensure all print() output is immediately flushed to CloudWatch (no buffering)
ENV PYTHONUNBUFFERED=1

# Enable verbose timing/diagnostic output (set to "false" via Lambda env override to disable)
ENV DOCLING_VERBOSE=true

# Set the Lambda handler
CMD ["lambda.handler.handler"]
