provider "aws" {
  region = "eu-west-2"
}

# ---- VPC AND SUBNETS ---
# # find vpc
# data "aws_vpc" "existing" {
#     default = true
# }

# # Fetch all private subnets inside that VPC across your eu-west-2 AZs
# data "aws_subnets" "private" {
#     filter {
#     name   = "vpc-id"
#     values = [data.aws_vpc.existing.id]
#     }
#     filter {
#     name   = "availability-zone"
#     values = ["eu-west-2a", "eu-west-2b", "eu-west-2c"]
#     }
# }


# ---- IAM ROLE POLICY -----

resource "aws_iam_role" "lambda_exec" {
  name = "hosp_proxy_lambda_execution_role"

  # Trust Policy: Allows Lambda service to assume this role
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

# ---- IAM IDENTITY POLICY ----

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
          "dynamodb:DeleteItem"
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

# Attach Policy to the Role
resource "aws_iam_role_policy_attachment" "attach_cache_policy" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = aws_iam_policy.lambda_dynamodb_cache.arn
}

# ---- CANARY & LISTENING RULES ----

resource "aws_lb_listener_rule" "canary_routing" {
  listener_arn = data.aws_lb_listener.existing_http.arn
  priority     = 1

  action {
    type = "forward"
    forward {
      target_group {
        arn    = aws_lb_target_group.legacy_rails.arn
        weight = 95
      }
      target_group {
        arn    = aws_lb_target_group.lambda_proxy.arn
        weight = 5
      }
    }
  }

  condition {
    path_pattern {
      values = ["/patients*"]
    }
  }
}



# ---- LAMBDA / ENV -----

resource "aws_lambda_function" "proxy_shield" {
  filename      = "lambda_payload.zip"
  function_name = "hosp_proxy_shield"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "index.lambda_handler"
  runtime       = "python3.12"
  timeout       = 12

  environment {
    variables = {
      CACHE_TABLE_NAME  = aws_dynamodb_table.cache.name
      HOSP_BACKEND_URL  = "http://13.40.197.254"
      CACHE_TTL_SECONDS = 20
    }
  }
}

# fmt
# put the tf state in the bucket somehow