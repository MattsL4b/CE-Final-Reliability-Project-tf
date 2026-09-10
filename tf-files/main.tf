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


data "aws_lb_listener" "existing_http" {
  load_balancer_arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:loadbalancer/app/lb-may26/3cf64897dfb55cc8"
  port              = 80
}

data "aws_lb_target_group" "hosp" {
  arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:targetgroup/lb-tg-may26/d7eac9179951f0ca"
}

# --- create the Lambda target group ---

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

# --- the canary rule ---

variable "proxy_weight" {
  type    = number
  default = 5 # start at 0, raise gradually
}

resource "aws_lb_listener_rule" "canary_routing" {
  listener_arn = data.aws_lb_listener.existing_http.arn
  priority     = 1 # must be an unused priority - check existing rules first

  action {
    type = "forward"

    forward {
      target_group {
        arn    = data.aws_lb_target_group.hosp.arn # existing HOSP TG, not a new "legacy_rails"
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
      values = ["/*"] # ALL paths. Use ["/patients*"] to limit the first canary's blast radius.
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