# Pre-Fargate Validation Checklist

Complete **every** item below before pressing the `workflow_dispatch` button on
`.github/workflows/validate-fargate.yml`. The workflow applies real AWS
infrastructure and will bill the account even if it fails mid-run and destroy
succeeds — confirm the cost guard-rails are live first.

---

## Cost guard-rails

- [ ] **AWS Budgets — $10 warning alert confirmed.**
  Navigate to AWS Budgets → confirm budget `task-scheduler-mcp-monthly-warn` exists
  with a $10 actual-spend threshold and an email subscriber.
  _Defined by `terraform/iam` module (slice #7a). If not yet applied, run:_
  ```bash
  cd terraform/iam
  terraform init -backend-config=../backend.tfvars -backend-config="key=iam/terraform.tfstate"
  terraform apply -var="budget_alert_email=<your@email.com>"
  ```

- [ ] **AWS Budgets — $30 cap alert confirmed.**
  Navigate to AWS Budgets → confirm budget `task-scheduler-mcp-monthly-cap` exists
  with a $30 actual-spend threshold and the same email subscriber.

- [ ] **Estimated cost reviewed.**
  A 30-minute run (the default) costs approximately **$0.50–$1.50**.
  A 120-minute run costs approximately **$3.00**.
  Calculation: 7 ECS Fargate tasks (0.25 vCPU / 512 MB) + 1 RDS `db.t3.micro`
  + 1 ALB + minor data transfer in ap-northeast-1.

---

## IAM / OIDC

- [ ] **OIDC role trust policy reviewed.**
  Verify in the AWS Console (IAM → Roles →
  `task-scheduler-mcp-github-actions-fargate-validation`) that the trust policy
  contains **only** `repo:PaynePew/task_scheduler_mcp:*` in the `sub` condition.
  No wildcard on the account or provider level.

- [ ] **GitHub secret `AWS_FARGATE_VALIDATION_ROLE_ARN` is set.**
  In the GitHub repository → Settings → Secrets and variables → Actions,
  confirm `AWS_FARGATE_VALIDATION_ROLE_ARN` is present and matches the role
  ARN shown in the IAM console above.

- [ ] **GitHub secret `TF_STATE_BUCKET` is set.**
  Should match the S3 bucket created by `terraform/bootstrap`
  (`task-scheduler-mcp-terraform-state-<ACCOUNT_ID>`).

- [ ] **GitHub secret `BUDGET_ALERT_EMAIL` is set.**
  Email address to receive AWS Budgets alerts.

---

## DNS swap plan

The smoke test (`curl https://<alb_dns>/healthz`) hits the ALB DNS directly,
so **no DNS swap is required** to close the validation.

If the demo recording requires traffic via `scheduler.paynepew.dev`:

1. Log in to Cloudflare dashboard → DNS.
2. Change the `scheduler` CNAME to the ALB DNS name shown in the workflow
   summary (available in the `fargate-validation-evidence` artifact →
   `tf-outputs.json` → `alb_dns_name`).
3. Allow up to 60 seconds for TTL propagation.
4. Record the demo.
5. Swap the CNAME back to the Lightsail IP (or its tunnel) immediately after.

Timing: do the DNS swap **only after** the ALB health check turns green
(workflow step 6 exits 0). The stack is available for `duration_minutes`
minutes before `terraform destroy` is triggered.

---

## Roll-back plan (if `terraform destroy` fails)

If the final destroy step fails and VPCs remain, perform this manual cleanup
in order. Each item can be checked in the AWS Console or via the CLI.

1. **ECS services** — AWS Console → ECS → Clusters → `task-scheduler-mcp` →
   Services. Set desired count to 0 for each service, then delete them.
   ```bash
   aws ecs list-services --cluster task-scheduler-mcp --query serviceArns --output text \
     | tr '\t' '\n' \
     | xargs -I{} aws ecs update-service --cluster task-scheduler-mcp --service {} --desired-count 0
   aws ecs delete-cluster --cluster task-scheduler-mcp
   ```

2. **ALB + target groups** — AWS Console → EC2 → Load Balancers → delete ALB
   with tag `Project=task-scheduler-mcp`. Then delete orphaned target groups.
   ```bash
   aws elbv2 describe-load-balancers \
     --query "LoadBalancers[?contains(LoadBalancerName,'task-scheduler-mcp')].LoadBalancerArn" \
     --output text | xargs aws elbv2 delete-load-balancer --load-balancer-arn
   ```

3. **RDS instance** — AWS Console → RDS → Databases → delete
   `task-scheduler-mcp-*` instance. Choose "Create final snapshot: No" and
   "Retain automated backups: No" to avoid lingering costs.
   ```bash
   aws rds describe-db-instances \
     --filters "Name=tag:Project,Values=task-scheduler-mcp" \
     --query "DBInstances[*].DBInstanceIdentifier" \
     --output text | xargs -I{} aws rds delete-db-instance \
       --db-instance-identifier {} --skip-final-snapshot \
       --delete-automated-backups
   ```

4. **SQS queues** — AWS Console → SQS → delete queues prefixed with
   `task-scheduler-mcp-`.
   ```bash
   aws sqs list-queues --queue-name-prefix task-scheduler-mcp \
     --query QueueUrls --output text | tr '\t' '\n' \
     | xargs -I{} aws sqs delete-queue --queue-url {}
   ```

5. **Security groups** — Delete all SGs with tag `Project=task-scheduler-mcp`.
   Note: SGs cannot be deleted while attached to running resources; confirm
   steps 1–2 are done first.
   ```bash
   aws ec2 describe-security-groups \
     --filters "Name=tag:Project,Values=task-scheduler-mcp" \
     --query "SecurityGroups[*].GroupId" \
     --output text | tr '\t' '\n' \
     | xargs -I{} aws ec2 delete-security-group --group-id {}
   ```

6. **RDS subnet group** — After RDS instance is gone:
   ```bash
   aws rds delete-db-subnet-group --db-subnet-group-name task-scheduler-mcp
   ```

7. **VPC** — Delete the VPC with tag `Project=task-scheduler-mcp`. The console will
   list dependencies that must be removed first (subnets, IGW, route tables).
   ```bash
   vpc_id=$(aws ec2 describe-vpcs \
     --filters "Name=tag:Project,Values=task-scheduler-mcp" \
     --query "Vpcs[0].VpcId" --output text)
   # Detach and delete IGW
   igw=$(aws ec2 describe-internet-gateways \
     --filters "Name=attachment.vpc-id,Values=${vpc_id}" \
     --query "InternetGateways[0].InternetGatewayId" --output text)
   aws ec2 detach-internet-gateway --internet-gateway-id "$igw" --vpc-id "$vpc_id"
   aws ec2 delete-internet-gateway --internet-gateway-id "$igw"
   # Delete subnets
   aws ec2 describe-subnets --filters "Name=vpc-id,Values=${vpc_id}" \
     --query "Subnets[*].SubnetId" --output text | tr '\t' '\n' \
     | xargs -I{} aws ec2 delete-subnet --subnet-id {}
   # Delete VPC
   aws ec2 delete-vpc --vpc-id "$vpc_id"
   ```

8. **IAM roles** — Only if `terraform/iam` destroy also failed:
   ```bash
   for role in task-scheduler-mcp-ecs-task-execution task-scheduler-mcp-ecs-task; do
     aws iam detach-role-policy --role-name "$role" \
       --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy \
       2>/dev/null || true
     aws iam delete-role --role-name "$role"
   done
   ```

After manual cleanup, re-run the final sanity check:
```bash
aws ec2 describe-vpcs \
  --filters "Name=tag:Project,Values=task-scheduler-mcp" \
  --query "Vpcs[*].VpcId" \
  --output text
# Expected: empty output
```

---

## Pre-dispatch sign-off

All items above checked → open `.github/workflows/validate-fargate.yml` →
**Run workflow** → set `duration_minutes` → click **Run workflow**.

Monitor the workflow run in GitHub Actions. The `fargate-validation-evidence`
artifact (with `tf-outputs.json`, `ecs-services.json`, `rds.json`) is
uploaded before the sleep step, so evidence is captured even if the destroy
fails.
