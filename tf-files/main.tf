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


}


# ---- LAMBDA
