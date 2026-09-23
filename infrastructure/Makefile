.PHONY: bootstrap synth diff deploy destroy

# Bootstrap the CDK toolkit in the target account/region (run once per account)
bootstrap:
	cdk bootstrap

# Synthesise CloudFormation templates without deploying
synth:
	cdk synth

# Show pending changes against the deployed stack
diff:
	cdk diff

# Deploy the stack (prompts for approval of security-sensitive changes)
deploy:
	cdk deploy

# Destroy the deployed stack (irreversible — use with caution)
destroy:
	cdk destroy
