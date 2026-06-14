provider "aws" {
  region = "us-east-1"
}

resource "aws_vpc" "demo" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "driftctl-demo", env = "demo" }
}

resource "aws_subnet" "demo" {
  vpc_id            = aws_vpc.demo.id
  cidr_block        = "10.0.1.0/24"
  availability_zone = "us-east-1a"
  tags = { Name = "driftctl-demo-subnet", env = "demo" }
}

resource "aws_security_group" "demo" {
  name        = "driftctl-demo-sg"
  description = "Demo security group for driftctl"
  vpc_id      = aws_vpc.demo.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "driftctl-demo-sg", env = "demo" }
}