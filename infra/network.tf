# VPC, subnets, egress, and the endpoints that let us minimise it.
#
# Public subnets hold only the ALB. Tasks and the database sit in private
# subnets with no public IP, which is what makes `assign_public_ip = false`
# possible on the ECS service and removes the $0.005/hour-per-ENI IPv4 charge.

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # required for interface endpoints to resolve

  tags = merge(local.tags, { Name = "${local.name}-vpc" })
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${local.name}-igw" })
}

# /20 public and /20 private per AZ out of a /16 — room to grow without
# renumbering, which is a rebuild rather than an edit.
resource "aws_subnet" "public" {
  count = var.az_count

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false # only the ALB lives here and it brings its own

  tags = merge(local.tags, {
    Name = "${local.name}-public-${count.index}"
    Tier = "public"
  })
}

resource "aws_subnet" "private" {
  count = var.az_count

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = merge(local.tags, {
    Name = "${local.name}-private-${count.index}"
    Tier = "private"
  })
}

# --- egress ---------------------------------------------------------------

resource "aws_eip" "nat" {
  count  = local.nat_gateway_count
  domain = "vpc"
  tags   = merge(local.tags, { Name = "${local.name}-nat-${count.index}" })
}

resource "aws_nat_gateway" "main" {
  count = local.nat_gateway_count

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(local.tags, { Name = "${local.name}-nat-${count.index}" })

  depends_on = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${local.name}-public" })
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.main.id
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# One private route table per AZ regardless of NAT mode. With `single` they all
# point at the same gateway; keeping the tables separate means switching to
# `per_az` later is a route change rather than a subnet re-association.
resource "aws_route_table" "private" {
  count = var.az_count

  vpc_id = aws_vpc.main.id
  tags   = merge(local.tags, { Name = "${local.name}-private-${count.index}" })
}

resource "aws_route" "private_nat" {
  count = local.nat_gateway_count > 0 ? var.az_count : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[local.nat_gateway_mode == "per_az" ? count.index : 0].id
}

resource "aws_route_table_association" "private" {
  count          = var.az_count
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# --- VPC endpoints --------------------------------------------------------

# Gateway endpoints are free and route S3 traffic off the NAT path entirely.
# Pulling ~1GB of image layers per deploy through NAT at $0.045/GB is the kind
# of charge that appears without anyone deciding to incur it.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.private[*].id

  tags = merge(local.tags, { Name = "${local.name}-s3" })
}

resource "aws_security_group" "endpoints" {
  count = local.enable_interface_endpoints ? 1 : 0

  name        = "${local.name}-endpoints"
  description = "Interface VPC endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "HTTPS from tasks"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }

  tags = merge(local.tags, { Name = "${local.name}-endpoints" })
}

# ecr.api authorises, ecr.dkr pulls layers, and layers themselves come from S3 —
# all three are needed or an image pull fails without NAT. logs and
# secretsmanager keep telemetry and credential fetches off the public path.
resource "aws_vpc_endpoint" "interface" {
  for_each = local.enable_interface_endpoints ? toset([
    "ecr.api",
    "ecr.dkr",
    "logs",
    "secretsmanager",
  ]) : toset([])

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.value}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true

  tags = merge(local.tags, { Name = "${local.name}-${each.value}" })
}

# --- security groups ------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public entry point"
  vpc_id      = aws_vpc.main.id

  egress {
    description = "To tasks"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-alb" })
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTPS"
  cidr_ipv4         = each.value
  from_port         = 443
  ip_protocol       = "tcp"
  to_port           = 443
}

# Port 80 exists only to redirect. It is still open to the world so that a
# plain http:// link works rather than timing out.
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  for_each = toset(var.alb_ingress_cidrs)

  security_group_id = aws_security_group.alb.id
  description       = "HTTP redirect to HTTPS"
  cidr_ipv4         = each.value
  from_port         = 80
  ip_protocol       = "tcp"
  to_port           = 80
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "ECS tasks"
  vpc_id      = aws_vpc.main.id

  # Outbound is open: the synthesis call goes to api.anthropic.com, whose
  # addresses are not a stable prefix worth pinning. Inbound is the constrained
  # direction and is handled by the rules below.
  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.tags, { Name = "${local.name}-tasks" })
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb_api" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "API port from the ALB only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.api_container_port
  ip_protocol                  = "tcp"
  to_port                      = var.api_container_port
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb_ui" {
  count = var.enable_ui ? 1 : 0

  security_group_id            = aws_security_group.tasks.id
  description                  = "UI port from the ALB only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = var.ui_container_port
  ip_protocol                  = "tcp"
  to_port                      = var.ui_container_port
}

resource "aws_security_group" "database" {
  name        = "${local.name}-db"
  description = "RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id

  tags = merge(local.tags, { Name = "${local.name}-db" })
}

# Source is the task security group, not a CIDR: the rule stays correct when
# subnets are renumbered, and nothing else in the VPC can reach the database.
resource "aws_vpc_security_group_ingress_rule" "database_from_tasks" {
  security_group_id            = aws_security_group.database.id
  description                  = "PostgreSQL from tasks"
  referenced_security_group_id = aws_security_group.tasks.id
  from_port                    = 5432
  ip_protocol                  = "tcp"
  to_port                      = 5432
}
