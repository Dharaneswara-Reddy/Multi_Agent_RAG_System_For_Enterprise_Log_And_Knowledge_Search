# Networking. The defining choice is what is absent: no NAT gateway.
#
# A NAT gateway is $32.85/month, more than the database and the task combined.
# It exists to give private subnets egress; this deployment puts the one thing
# needing egress — a Fargate task pulling from ECR — in a public subnet with a
# public IP, and keeps the database private with no route off the VPC at all.
#
# The task's public address is not reachable: its security group admits traffic
# from the load balancer's security group only.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

# --- public: the load balancer and the task -------------------------------
#
# Two, because an ALB requires subnets in at least two availability zones and
# refuses to create otherwise. The task runs in one of them; the second exists
# for the load balancer's benefit and costs nothing.

resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = local.public_subnets[count.index]
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count = length(aws_subnet.public)

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- private: the database only --------------------------------------------

resource "aws_subnet" "private" {
  count = length(local.private_subnets)

  vpc_id            = aws_vpc.main.id
  cidr_block        = local.private_subnets[count.index]
  availability_zone = local.azs[count.index]

  tags = { Name = "${local.name}-private-${count.index}" }
}

# No routes beyond the VPC-local one. With no NAT and no gateway route the
# database subnets have no path off the VPC in either direction.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = length(aws_subnet.private)

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# Free, unlike every interface endpoint. Keeps corpus and index traffic on the
# AWS network rather than out through the internet gateway.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id, aws_route_table.private.id]

  tags = { Name = "${local.name}-s3" }
}

# --- security groups --------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Load balancer. Ingress from CloudFront only."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-alb" }
}

# CloudFront's ranges, not 0.0.0.0/0. The distribution is where TLS terminates,
# so reaching the ALB directly would be a downgrade to plaintext — this makes
# that impossible rather than merely discouraged. Looked up by name because AWS
# rotates the list's contents.
resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP from CloudFront origin-facing ranges"
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront_origins.id
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To the task only"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "Fargate task. Ingress from the load balancer only."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-tasks" }
}

# The task holds a public IP so it can reach ECR without a NAT gateway, but
# this is the only way in, and it does not include the internet.
resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Streamlit from the load balancer"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.container_port
  to_port                      = var.container_port
  ip_protocol                  = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "tasks_all" {
  security_group_id = aws_security_group.tasks.id
  description       = "ECR, SSM, CloudWatch, S3, PostgreSQL, api.anthropic.com"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_security_group" "database" {
  name = "${local.name}-db"
  # A security group's description is immutable, so editing this string forces
  # a replacement — and a replacement cannot complete while RDS holds an ENI on
  # the group, because service-managed interfaces cannot be detached. The apply
  # fails with a misleading "AuthFailure ... DetachNetworkInterface".
  #
  # Left at the original wording deliberately. "Application" and "task" mean the
  # same thing here, and the accurate-but-identical string is worth more than a
  # marginally better one that costs a database outage to land.
  description = "PostgreSQL. Reachable only from the application security group."
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${local.name}-db" }
}

resource "aws_vpc_security_group_ingress_rule" "database_from_tasks" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from the application only"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

# No egress rule for the database. It never initiates a connection, and the
# absence of a rule is what enforces that.
