# The load balancer. $16.43/month, and it is not optional in this design:
# Fargate tasks get a new address every time they are replaced, so something
# with a stable name has to sit in front. That is the cost the EC2 variant
# avoided by holding an Elastic IP, and it is why the EC2 variant was cheaper
# right up until the Free Plan refused to launch an instance large enough.
#
# It is a load balancer with one target. It is not high availability and it is
# not autoscaling.

resource "aws_lb" "main" {
  name               = local.name
  load_balancer_type = "application"
  internal           = false
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.alb.id]

  # A demo stack has to be destroyable without ceremony.
  enable_deletion_protection = false

  # Streamlit holds a WebSocket open for the life of the session. The default
  # 60s idle timeout would drop an idle browser tab and present as the console
  # mysteriously disconnecting.
  idle_timeout = 300

  tags = { Name = local.name }
}

resource "aws_lb_target_group" "app" {
  name        = local.name
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip" # awsvpc networking: targets are ENIs, not instances

  health_check {
    path                = "/_stcore/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }

  # Streamlit is stateful per session: the browser holds a WebSocket to one
  # task and reconnecting to another loses the session. Irrelevant with a
  # single task, set correctly anyway so raising desired_count does not
  # silently break the console.
  stickiness {
    type            = "lb_cookie"
    cookie_duration = 86400
    enabled         = true
  }

  # Let a replacement register before the old target is torn out.
  deregistration_delay = 30

  tags = { Name = local.name }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # HTTP, because CloudFront terminates TLS and there is no custom domain to
  # issue an ACM certificate against. The ALB's security group admits
  # CloudFront's ranges only, so this listener is not reachable from the
  # internet at large.
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}
