import os
import boto3
from dotenv import load_dotenv

load_dotenv()

bucket = os.getenv("S3_BUCKET")
s3 = boto3.client(
    "s3",
    region_name=os.getenv("AWS_REGION"),
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
)

key = "test/hello.txt"

s3.put_object(
    Bucket=bucket,
    Key=key,
    Body=b"hello from my pipeline",
    Metadata={"tool": "test", "source": "local"},
)
print("Uploaded:", key)

obj = s3.get_object(Bucket=bucket, Key=key)
print("Read back:", obj["Body"].read().decode())
print("Metadata:", obj["Metadata"])