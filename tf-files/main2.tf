provider "aws" {
  region = "eu-west-2"
}


# ---- REFERENCE EXISTING ALB -----

data "aws_lb" "existing_alb" {
  arn = "arn:aws:elasticloadbalancing:eu-west-2:664047078509:loadbalancer/app/lb-may26/3cf64897dfb55cc8"
}


# ---- CLOUDFRONT DISTRIBUTION ----

resource "aws_cloudfront_distribution" "api_cache_shield" {
  enabled         = true
  is_ipv6_enabled = true
  comment         = "CloudFront Edge Cache & Shield for HOSP Legacy Rails API"

  # Origin: Points directly to your existing ALB public DNS
  origin {
    domain_name = data.aws_lb.existing_alb.dns_name
    origin_id   = "HOSP-ALB-Origin"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only" # Connects to ALB on Port 80
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  # Default Cache Behavior (Handles GET caching & routes POST/PUT directly)
  default_cache_behavior {
    allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = "HOSP-ALB-Origin"

    # Forwards headers, cookies, and query strings so Rails authentication works seamlessly
    forwarded_values {
      query_string = true
      headers      = ["Authorization", "Host", "Accept", "Content-Type"]

      cookies {
        forward = "all"
      }
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = 0
    default_ttl            = 20  # Cache GET responses for 20 seconds
    max_ttl                = 60
    compress               = true
  }

  # Restrictions (No geographic locking required)
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # SSL Certificate Configuration (Uses default CloudFront certificate)
  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Environment = "production"
    Service     = "hosp-edge-shield"
  }
}


# ---- OUTPUTS ----

output "cloudfront_domain_name" {
  value       = aws_cloudfront_distribution.api_cache_shield.domain_name
  description = "Point your API domain DNS (CNAME/Alias) to this address"
}