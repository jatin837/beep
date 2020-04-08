"""
Module to provide connection objects to various postgres environments.

Current environments:
- local     Local development environment. Non-secret credentials.
- stage     Beep stage environment. Credentials to database instance are
            obtained through AWS SecretsManager.

"""

import boto3
import base64
from botocore.exceptions import ClientError
import json

from beep.config import config


def secret_accessible(environment):
    pg_config = config[environment]['postgres']
    if 'secret' in pg_config:
        secret_name = pg_config['secret']
        try:
            _ = get_secret(secret_name)
        except Exception as e:
            return False
        else:
            return True
    else:
        return True


def get_secret(secret_name):
    """
    Returns the secret for the beep database and respective environment.

    Args:
        secret_name:    str representing the location in secrets manager

    Returns:
        secret          dict object containing database credentials

    """
    region_name = 'us-west-2'

    # Create a Secrets Manager client
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        raise e
    else:
        # Decrypts secret using the associated KMS CMK.
        # Depending on whether the secret is a string or binary,
        # one of these fields will be populated.
        if 'SecretString' in get_secret_value_response:
            secret = get_secret_value_response['SecretString']
            return json.loads(secret)
        else:
            decoded_binary_secret = base64.b64decode(
                get_secret_value_response['SecretBinary'])
            return json.loads(decoded_binary_secret)
