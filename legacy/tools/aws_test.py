from legacy.providers.aws import driver as aws
import time
timestamp = int(time.time())
prefix = f'aws-{timestamp}'
log_dir = f'{prefix}-logs'
aws.run_aws_scamper(log_dir, prefix)
