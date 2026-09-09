provider "aws" {
    region = "eu-west-2"
}

# ---- VPC AND SUBNETS ---
# find vpc
data "aws_vpc" "existing" {
    default = true
}

# Fetch all private subnets inside that VPC across your eu-west-2 AZs
data "aws_subnets" "private" {
    filter {
    name   = "vpc-id"
    values = [data.aws_vpc.existing.id]
    }
    filter {
    name   = "availability-zone"
    values = ["eu-west-2a", "eu-west-2b", "eu-west-2c"]
    }
}

# ---- IAM USER ----
resource "aws_iam_user" "tf" {
    name = "may26-tf"
    tags = {
        team = "may26"
        purpose = "terraform"
    }
}

resource "aws_iam_user_group_membership" "tf" {
    user = aws_iam_user.tf.name
    groups = ["students"]
}

resource "aws_iam_access_key" "tf" {
    user = aws_iam_user.tf.name
}

output "may26_tf_access_key_id" {
    value = aws_iam_access_key.tf.id
}

output "may26_tf_secret_access_key" {
    value = aws_iam_access_key.tf.secret
    sensitive = true
}


# ---- LAMBDA