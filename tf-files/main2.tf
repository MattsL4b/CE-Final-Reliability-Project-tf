provider "aws" {
  region = "eu-west-2"
}

# ---- IAM ROLE & POLICIES FOR LAMBDA ----

resource "aws_iam_role" "lambda_exec" {
  name = "hosp_proxy_lambda_execution_role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

# DynamoDB & CloudWatch Access Policy
resource "aws_iam_policy" "lambda_dynamodb_cache" {
  name        = "hosp_proxy_dynamodb_cache_policy"
  description = "Allows proxy Lambda to read, write, and invalidate DynamoDB cache items"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem",
          "dynamodb:Scan"
        ]
        Resource = aws_dynamodb_table.cache.arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "attach_cache_policy" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_dynamodb_cache.arn
}

# [ADDED] Required permissions for Lambda to run inside a VPC (ENI management)
resource "aws_iam_role_policy_attachment" "lambda_vpc_access" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service/role/AWSLambdaVPCAccessExecutionRole"
}


# ---- VPC NETWORKING & SECURITY GROUPS ----


# [ADDED] Dedicated Security Group for the Lambda proxy inside the HOSP VPC
resource "aws_security_group" "lambda_sg" {
  name        = "may26-lambda-proxy-sg"
  description = "Security group for Lambda proxy shield"
  vpc_id      = "vpc-080dbb0b7dc86503a"

  # Outbound HTTP access to HOSP private IP
  egress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["172.31.39.164/32"]
  }

  # Outbound HTTPS for DynamoDB / AWS APIs
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "may26-lambda-proxy-sg"
  }
}

# [ADDED] VPC Gateway Endpoint so private subnets can reach DynamoDB without internet
resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = "vpc-080dbb0b7dc86503a"
  service_name      = "com.amazonaws.eu-west-2.dynamodb"
  vpc_endpoint_type = "Gateway"

  # Note: Add private route table ID(s) here if known
  # route_table_ids = ["rtb-xxxxxxxxxxxxxxxxx"]

  tags = {
    Name = "dynamodb-vpc-endpoint"
  }
}


# ---- ALB TARGET GROUP & CANARY ROUTING ----

data "aws_lb_listener" "existing_http" {
  load_balancer_arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:loadbalancer/app/lb-may26/3cf64897dfb55cc8"
  port              = 80
}

data "aws_lb_target_group" "hosp" {
  arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:targetgroup/lb-tg-may26/d7eac9179951f0ca"
}

resource "aws_lb_target_group" "lambda_proxy" {
  name        = "may26-proxy-tg"
  target_type = "lambda"
}

resource "aws_lambda_permission" "alb" {
  statement_id  = "AllowALBInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.proxy_shield.function_name
  principal     = "elasticloadbalancing.amazonaws.com"
  source_arn    = aws_lb_target_group.lambda_proxy.arn
}

resource "aws_lb_target_group_attachment" "lambda_proxy" {
  target_group_arn = aws_lb_target_group.lambda_proxy.arn
  target_id        = aws_lambda_function.proxy_shield.arn
  depends_on       = [aws_lambda_permission.alb]
}

variable "proxy_weight" {
  type    = number
  default = 0 # start at 0, raise gradually
}

resource "aws_lb_listener_rule" "canary_routing" {
  listener_arn = data.aws_lb_listener.existing_http.arn
  priority     = 2

  action {
    type = "forward"

    forward {
      target_group {
        arn    = data.aws_lb_target_group.hosp.arn
        weight = 100 - var.proxy_weight
      }

      target_group {
        arn    = aws_lb_target_group.lambda_proxy.arn
        weight = var.proxy_weight
      }

      stickiness {
        enabled  = false
        duration = 1
      }
    }
  }

  condition {
    path_pattern {
      values = ["/*"]
    }
  }
}


# ---- LAMBDA FUNCTION & ENVIRONMENT ----

resource "aws_lambda_function" "proxy_shield" {
  filename      = "lambda_payload.zip"
  function_name = "hosp_proxy_shield"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "index.lambda_handler"
  runtime       = "python3.12"
  timeout       = 30

  # [ADDED] VPC Placement Configuration
  vpc_config {
    subnet_ids = [
      "subnet-09f2ffa366a8abe67",
      "subnet-0fc0a94296b831a31"
    ]
    security_group_ids = [aws_security_group.lambda_sg.id]
  }

  environment {
    variables = {
      CACHE_TABLE_NAME  = aws_dynamodb_table.cache.name
      HOSP_BACKEND_URL  = "http://172.31.39.164" # [UPDATED] Switched to Private IP
      CACHE_TTL_SECONDS = "20"
    }
  }

  # [ADDED] Ensure ENI policy is attached before creating function
  depends_on = [
    aws_iam_role_policy_attachment.lambda_vpc_access
  ]
}


# ---- OUTPUTS FOR COACH REQUEST ----


# [ADDED] Copy this value after 'terraform apply' and send to coach
output "lambda_security_group_id" {
  value       = aws_security_group.lambda_sg.id
  description = "Provide this SG ID to coach to add HTTP :80 ingress rule on HOSP SG"
}