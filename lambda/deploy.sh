# Set variables
AWS_REGION=us-west-2
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO_NAME=872855011810.dkr.ecr.us-west-2.amazonaws.com/icg-document-enrichment
IMAGE_TAG=0.1.16
FUNCTION_NAME=icg-document-enrichment

echo "Building Docker image..."

# --provenance=false: Disables attestation manifests that use OCI media types
# AWS Lambda does not support; prevents "image manifest, config or layer media type ... is not supported"
# docker build --no-cache \
#   --platform linux/amd64 \
#   --provenance=false \
#   -t ${ECR_REPO_NAME}:${IMAGE_TAG} \
#   -f lambda/Dockerfile .

# echo "Pushing Docker image to ECR..."
docker push ${ECR_REPO_NAME}:${IMAGE_TAG}

IMAGE_URI=${ECR_REPO_NAME}:${IMAGE_TAG}

if aws lambda get-function --function-name $FUNCTION_NAME --region $AWS_REGION >/dev/null 2>&1; then
  echo "Updating existing Lambda function..."
  aws lambda update-function-code \
    --function-name $FUNCTION_NAME \
    --image-uri $IMAGE_URI \
    --region $AWS_REGION
else
  echo "Creating Lambda function..."
  aws lambda create-function \
    --function-name $FUNCTION_NAME \
    --package-type Image \
    --code "ImageUri=${IMAGE_URI}" \
    --role "arn:aws:iam::${AWS_ACCOUNT_ID}:role/lambda-hydrator-exec" \
    --timeout 900 \
    --memory-size 4124 \
    --region $AWS_REGION \
    --architectures x86_64
fi
