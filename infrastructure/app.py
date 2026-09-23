#!/usr/bin/env python3
"""
CDK App entry point for the Serverless Weather App.

Instantiates and deploys the WeatherAppStack which provisions all
core AWS infrastructure: API Gateway, Lambda, DynamoDB, Secrets Manager,
CloudWatch logs, and IAM roles.
"""
import os

import aws_cdk as cdk

from weather_app_stack import WeatherAppStack

app = cdk.App()

WeatherAppStack(
    app,
    "WeatherAppStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Serverless Weather App — API Gateway, Lambda, DynamoDB, Secrets Manager",
)

app.synth()
