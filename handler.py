#!/usr/bin/env python3
"""
This is a set of custom CloudFormation resources to help make deployments
easier.
"""
# pylint: disable=C0103
from base64 import b16encode, b64decode, b64encode
from distutils.util import strtobool        # pylint: disable=E0401,E0611
from json import dumps as json_dumps
from logging import getLogger, DEBUG
from os import urandom
from typing import Any, Dict
from uuid import uuid4

from amifilter import add_filters, filter_names_and_descriptions
import boto3
from passlib.pwd import genphrase, genword
from iso8601 import parse_date
import requests
from hashparams import HashAlgorithm, HashParameter

log = getLogger()
log.setLevel(DEBUG)

def listify(value: Any):
    """
    Encapsulate value in a list if it isn't already.
    """
    if isinstance(value, list):
        return value

    return [value]

def lambda_handler(event: Dict[str, Any], _) -> None:
    """
    Main entrypoint for the Lambda function.
    """
    log.debug("event=%s", event)

    body = {
        "Status": "FAILED",
        "Reason": "Unknown error",
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
    }

    if "PhysicalResourceId" in event:
        body["PhysicalResourceId"] = event["PhysicalResourceId"]

    handler = handlers.get(event["ResourceType"])
    if handler is None:
        body["Reason"] = "Unknown resource type %s" % event["ResourceType"]
    else:
        try:
            data = handler(event)
            if data is None:
                data = {}
            if "PhysicalResourceId" in data: # pragma: nocover
                body["PhysicalResourceId"] = data.pop("PhysicalResourceId")
            body["Status"] = "SUCCESS"
            del body["Reason"]
            body["Data"] = data
        except Exception as e:              # pylint: disable=W0703
            log.error("Failed", exc_info=1)
            body["Reason"] = str(e)

    if "PhysicalResourceId" not in body:
        body["PhysicalResourceId"] = str(uuid4())

    log.debug("body=%s", body)
    body = json_dumps(body)
    headers = {
        "Content-Type": "",
        "Content-Length": str(len(body)),
    }
    r = requests.put(event["ResponseURL"], headers=headers, data=body)
    print("Result: %d %s" % (r.status_code, r.reason))
    return


def api_gateway_binary(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Custom::ApiGatewayBinary resource
    Enable binary support on an API Gateway REST API.
    """
    apigw = boto3.client("apigateway")
    request_type = event["RequestType"]
    rest_api_id = event["ResourceProperties"]["RestApiId"]

    # Do we already have binary support enabled?
    rest_api_info = apigw.get_rest_api(restApiId=rest_api_id)
    binary_enabled = ("binaryMediaTypes" in rest_api_info and
                      "*/*" in rest_api_info["binaryMediaTypes"])

    if request_type in ("Create", "Update"):
        if not binary_enabled:
            apigw.update_rest_api(restApiId=rest_api_id, patchOperations=[
                {"op": "add", "path": "/binaryMediaTypes/*~1*"}
            ])
    elif request_type == "Delete":
        if binary_enabled:
            apigw.update_rest_api(restApiId=rest_api_id, patchOperations=[
                {"op": "remove", "path": "/binaryMediaTypes/*~1*"}
            ])

    return


def find_image(event):
    """
    Custom::FindImage resource
    Locates the latest version of an AMI/AKI/ARI with given attributes.
    """
    if event["RequestType"] not in ("Create", "Update"):
        return

    rp = dict(event["ResourceProperties"])
    filters = {}

    try:
        owner = rp["Owner"]
    except KeyError:
        raise ValueError("Owner must be specified")

    add_filters(rp, filters)

    # Convert the filters dict to a list of {Name: key, Value: values} dicts
    ec2_filters = [{"Name": key, "Values": values}
                   for key, values in filters.items()]

    ec2 = boto3.client("ec2")
    result = ec2.describe_images(Owners=[owner], Filters=ec2_filters)
    images = result.get("Images")

    if not images:
        raise ValueError("No AMIs found that match the filters applied.")

    images = filter_names_and_descriptions(images, rp)

    preferred_virtualization_type = rp.get("PreferredVirtualizationType")
    preferred_root_device_type = rp.get("PreferredRootDeviceType")

    def sort_key(image):
        """
        Prioritize AMI preferences.
        """
        date = parse_date(image["CreationDate"])
        is_preferred_virtualization_type = (
            preferred_virtualization_type is None or
            image["VirtualizationType"] == preferred_virtualization_type)
        is_preferred_root_device_type = (
            preferred_root_device_type is None or
            image["RootDeviceType"] == preferred_root_device_type)

        return (is_preferred_virtualization_type,
                is_preferred_root_device_type,
                date)

    images.sort(key=sort_key, reverse=True)
    image_ids = [image["ImageId"] for image in images]
    return {
        "ImageId": image_ids[0],
        "MatchingImageIds": image_ids,
    }


def generate_password(event):
    """
    Custom::GeneratePassword resource
    Generate a password using passlib.
    """
    if event["RequestType"] not in ("Create", "Update"):
        return

    rp = dict(event["ResourceProperties"])

    password_type = rp.get("PasswordType", "word")
    kw = {}

    if password_type == "phrase":
        generator = genphrase
        handle_genphrase_properties(rp, kw)
    elif password_type == "word":
        generator = genword
        handle_genword_properties(rp, kw)
    else:
        raise ValueError(
            'PasswordType must be "word" or "phrase": %r' % password_type)

    entropy = rp.get("Entropy")
    if entropy is not None:
        if not isinstance(entropy, int):
            raise ValueError('Entropy must be an integer: %r' % entropy)
        kw["entropy"] = entropy

    password = generator(**kw)

    encryption_key = rp.get("EncryptionKey")
    if encryption_key is not None:
        encryption_context = rp.get("EncryptionContext", {})

        kms = boto3.client("kms")
        result = kms.encrypt(
            KeyId=encryption_key, EncryptionContext=encryption_context,
            Plaintext=password.encode("utf-8"))

        blob = result["CiphertextBlob"]
        result = {"CiphertextBase64Password": b64encode(blob).decode("utf-8")}
    else:
        result = {"PlaintextPassword": password}

    return result

def handle_genphrase_properties(
        request_properties: Dict[str, Any], generator_kw: Dict[str, Any]) \
        -> None:
    """
    handle_genphrase_properties(
        request_properties: Dict[str, Any], generator_kw: Dict[str, Any]) -> None
    Convert request properties to keyword parameters for the generator.
    """
    if "Chars" in request_properties:
        raise ValueError(
            'Chars cannot be specified when PasswordType is "phrase"')

    if "Charset" in request_properties:
        raise ValueError(
            'Charset cannot be specified when PasswordType is "phrase"')

    if "Wordset" in request_properties:
        if "Words" in request_properties:
            raise ValueError(
                'Words and Wordset are mutually exclusive')
        generator_kw["wordset"] = request_properties["Wordset"]
    elif "Words" in request_properties:
        generator_kw["words"] = request_properties["Words"]

    if "Separator" in request_properties:
        generator_kw["sep"] = request_properties["Separator"]

    return

def handle_genword_properties(
        request_properties: Dict[str, Any], generator_kw: Dict[str, Any]) \
        -> None:
    """
    handle_genword_properties(
        request_properties: Dict[str, Any], generator_kw: Dict[str, Any]) -> None
    Convert request properties to keyword parameters for the generator.
    """
    if "Words" in request_properties:
        raise ValueError(
            'Words cannot be specified when PasswordType is "word"')

    if "Wordset" in request_properties:
        raise ValueError(
            'Wordset cannot be specified when PasswordType is "word"')

    if "Charset" in request_properties:
        if "Chars" in request_properties:
            raise ValueError(
                'Chars and Charset are mutually exclusive')
        generator_kw["charset"] = request_properties["Charset"]
    elif "Chars" in request_properties:
        generator_kw["chars"] = request_properties["Chars"]

    if "Separator" in request_properties:
        raise ValueError(
            'Separator cannot be specified when PasswordType is "word"')

    return

def hash_password(event):
    """
    Custom::HashPassword resource

    Hash a password. See the passlib documentation for more details.

    Note: Some of the hashing schemes are now considered insecure, but are
    included because various legacy products require them. To use an insecure
    hashing mechanism, the AllowInsecure property must be set to true.
    """
    if event["RequestType"] not in ("Create", "Update"):
        return

    rp = dict(event["ResourceProperties"])

    # Make sure we have exactly one of plaintext_password or ciphertext_b64_password
    if "PlaintextPassword" in rp and "CiphertextBase64Password" in rp:
        raise ValueError(
            "PlaintextPassword and CiphertextBase64Password are mutually "
            "exclusive")

    if "PlaintextPassword" not in rp and "CiphertextBase64Password" not in rp:
        raise ValueError(
            "Either PlaintextPassword or CiphertextBase64Password must be "
            "specified")

    if "PlaintextPassword" in rp:
        plaintext_password = handle_plaintext_password_hash_params(rp)
    else:
        plaintext_password = handle_ciphertext_password_hash_params(rp)

    algorithm = get_hash_algorithm(rp)

    # Parse algorithm-specific parameters
    builder = algorithm.algorithm
    builder_kw = {}

    for parameter_name, parameter in algorithm.parameters.items():
        if parameter_name not in rp:
            continue

        parameter_value = rp.pop(parameter_name)
        parameter_value = validate_hash_parameter(
            parameter, parameter_name, parameter_value)
        builder_kw[parameter.algorithm_parameter] = parameter_value

    rp.pop("ServiceToken", "")
    if rp:
        raise ValueError("Unknown parameters: %s" %
                         ", ".join(sorted(rp.keys())))

    builder = builder.using(**builder_kw)
    result = builder.hash(plaintext_password)

    return {"Hash": result}

def handle_plaintext_password_hash_params(
        request_properties: Dict[str, Any]) -> str:
    """
    handle_plaintext_password_hash_params(
            request_properties: Dict[str, Any]) -> str
    Handle the PlaintextPassword case of Custom::HashPassword
    """
    plaintext_password = request_properties.pop("PlaintextPassword")
    if not isinstance(plaintext_password, str):
        raise TypeError("PlaintextPassword must be a string")

    return plaintext_password

def handle_ciphertext_password_hash_params(
        request_properties: Dict[str, Any]) -> str:
    """
    handle_ciphertext_password_hash_params(
            request_properties: Dict[str, Any]) -> str
    Handle the CiphertextBase64Password case of Custom::HashPassword,
    returning the plaintext password.
    """
    ciphertext_b64_password = request_properties.pop("CiphertextBase64Password")
    encryption_context = request_properties.pop("EncryptionContext", None)
    if not isinstance(ciphertext_b64_password, str):
        raise TypeError(
            "CiphertextBase64Password must be a string")

    if encryption_context is None:
        encryption_context = {}
    elif not isinstance(encryption_context, dict):
        raise TypeError("EncryptionContext must be a mapping")

    try:
        ciphertext = b64decode(ciphertext_b64_password)
    except ValueError:
        raise ValueError(
            "Invalid base64 encoding in CiphertextBase64Password")

    try:
        result = boto3.client("kms").decrypt(
            CiphertextBlob=ciphertext,
            EncryptionContext=encryption_context)
    except Exception:
        raise ValueError(
            "Unable to decrypt CiphertextBase64Password")

    return result["Plaintext"]


def get_hash_algorithm(
        request_properties: Dict[str, Any]) -> HashAlgorithm:
    """
    get_hash_algorithm(
        request_properties: Dict[str, Any]) -> HashAlgorithm
    Find a hash algorithm for the Scheme request property.
    """
    scheme = request_properties.pop("Scheme", None)
    allow_insecure = request_properties.pop("AllowInsecure", False)
    if isinstance(allow_insecure, str):
        allow_insecure = strtobool(allow_insecure)
    elif isinstance(allow_insecure, (list, tuple, dict)):
        raise TypeError("AllowInsecure must be true or false")
    else:
        allow_insecure = bool(allow_insecure)

    # Make sure Scheme was specified and is valid.
    if scheme is None:
        raise ValueError("Scheme must be specified")
    elif not isinstance(scheme, str):
        raise TypeError("Scheme must be a string")
    elif not scheme:
        raise ValueError("Scheme cannot be empty")

    algorithm = HashAlgorithm.algorithms.get(scheme.replace("-", "_"))
    if algorithm is None:
        raise ValueError("Unknown scheme %r" % scheme)

    # Don't allow insecure algorithms if AllowInsecure wasn't specified.
    if not algorithm.is_secure and not allow_insecure:
        raise ValueError(
            "Scheme %s is insecure and AllowInsecure was not specified" %
            scheme)

    return algorithm

def validate_hash_parameter(parameter: HashParameter, name: str,
                            value: Any) -> Any:
    """
    validate_hash_parameter(parameter: HashParameter, name: str,
                            value: Any) -> Any
    Make sure a parameter value passes all of its constraints.
    """
    try:
        value = parameter.type(value)
    except (TypeError, ValueError):
        raise ValueError(
            "Invalid value for parameter %s: %r" % (name, value))

    if parameter.validator is not None:
        parameter.validator(value)

    if parameter.min_length is not None and len(value) < parameter.min_length:
        raise ValueError(
            "Length of parameter %s cannot be less than %s: %r" %
            (name, parameter.min_length, value))

    if parameter.max_length is not None and len(value) > parameter.max_length:
        raise ValueError(
            "Length of parameter %s cannot be greater than %s: %r" %
            (name, parameter.max_length, value))

    if parameter.min_value is not None and value < parameter.min_value:
        raise ValueError(
            "Value of parameter %s cannot be less than %s: %r" %
            (name, parameter.min_value, value))

    if parameter.max_value is not None and value > parameter.max_value:
        raise ValueError(
            "Value of parameter %s cannot be greater than %s: %r" %
            (name, parameter.max_value, value))

    return value


def secure_random(event):
    """
    Custom::SecureRandom resource
    Securely generated bytes, base-64 encoded.
    """
    if event["RequestType"] not in ("Create", "Update"):
        return

    size = event["ResourceProperties"]["Size"]
    try:
        size = int(size)
        if size <= 0:
            raise ValueError()
    except ValueError:
        raise ValueError("Invalid size parameter: %r" % (size,))

    result = urandom(size)
    return {
        "Raw": result.decode("iso8859-1"),
        "Hex": b16encode(result).decode("ascii").lower(),
        "Base64": b64encode(result).decode("ascii"),
    }


handlers = {
    "Custom::ApiGatewayBinary": api_gateway_binary,
    "Custom::FindImage": find_image,
    # "Custom::DynamoDB::Item": dynamodb_item,
    "Custom::GeneratePassword": generate_password,
    "Custom::HashPassword": hash_password,
    "Custom::SecureRandom": secure_random,
}
