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

# Set environment variables for model caching
ENV HF_HOME=/tmp/
ENV TORCH_HOME=/tmp/

# Download Docling models during build (will be cached in the image)
RUN docling-tools models download

# Copy application code
COPY rest ${LAMBDA_TASK_ROOT}/rest
COPY lambda/handler.py ${LAMBDA_TASK_ROOT}/lambda/handler.py
COPY docling ${LAMBDA_TASK_ROOT}/docling

# On container environments, always set a thread budget to avoid undesired thread congestion.
ENV OMP_NUM_THREADS=4

# Set the Lambda handler
CMD ["lambda.handler.handler"]
