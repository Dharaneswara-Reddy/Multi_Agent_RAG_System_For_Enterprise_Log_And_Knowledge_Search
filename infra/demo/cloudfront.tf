# CloudFront is the public endpoint. With an ALB now in the design, the obvious
# question is whether it still earns its place — the load balancer already has
# a stable DNS name. It does, for one reason that is not about caching:
#
#   **TLS.** An ALB serves HTTPS only with a certificate, ACM issues
#   certificates only against a domain you control, and there is no custom
#   domain here. Without CloudFront the public URL is plain `http://` — a
#   browser warning on a link handed to recruiters. CloudFront supplies a
#   trusted certificate on *.cloudfront.net for nothing.
#
# It also gives a second benefit worth having: the ALB's security group admits
# CloudFront's ranges only, so nobody can reach the load balancer directly and
# downgrade to plaintext. And it is free at this traffic — 1 TB/month.
#
# What it is NOT is a load balancer. That is the ALB's job, and even the ALB
# has exactly one target here, so neither component makes this deployment
# highly available.

data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer_except_host" {
  name = "Managed-AllViewerExceptHostHeader"
}

resource "aws_cloudfront_distribution" "app" {
  enabled         = true
  comment         = "${local.name} — Streamlit console"
  price_class     = "PriceClass_100" # North America + Europe; the cheapest tier
  http_version    = "http2and3"
  is_ipv6_enabled = true

  origin {
    origin_id = "app"

    # The load balancer's DNS name. Stable across task replacement, which is
    # the problem it solves — a Fargate task's address changes every time it is
    # replaced, and CloudFront rejects a bare IP as a custom origin anyway.
    domain_name = aws_lb.main.dns_name

    custom_origin_config {
      http_port  = 80
      https_port = 443

      # HTTP to the origin. The instance has no certificate, and a self-signed
      # one buys nothing because CloudFront would have to be told to skip
      # validation anyway. The exposure is the CloudFront-to-origin hop inside
      # AWS's network, mitigated by the security group admitting only
      # CloudFront. Documented rather than hidden: this is the weakest link in
      # the demo architecture, and the production root terminates TLS at an ALB
      # instead.
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]

      # Generous, because a cold container loading two ONNX models and pulling
      # the index from S3 can take well over a minute to answer the first
      # request. The default 30s would surface as a 504 to whoever arrives first.
      origin_read_timeout      = 60
      origin_keepalive_timeout = 60
    }
  }

  default_cache_behavior {
    target_origin_id       = "app"
    viewer_protocol_policy = "redirect-to-https"

    # Streamlit is a WebSocket application: the browser loads a shell over HTTP
    # and then upgrades to /_stcore/stream, over which every interaction flows.
    # GET/HEAD alone would load a page that never becomes interactive.
    allowed_methods = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods  = ["GET", "HEAD"]
    compress        = true

    # Looked up by name rather than pasted as UUIDs. The ids are stable, but a
    # transposed character in a hardcoded one fails at apply with "no such
    # policy" and nothing to grep for.
    #
    # CachingDisabled: every response here is session state. Caching would serve
    # one visitor's answer to the next.
    cache_policy_id = data.aws_cloudfront_cache_policy.disabled.id
    # AllViewerExceptHostHeader forwards cookies, query strings and the
    # Upgrade/Connection headers the WebSocket handshake needs, while replacing
    # Host with the origin's own name. Plain AllViewer would forward the
    # viewer's Host, and the request would arrive at the origin for a hostname
    # it does not answer to.
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    # The default *.cloudfront.net certificate. A custom domain would need an
    # ACM certificate in us-east-1 and a Route 53 zone at $0.50/month; the
    # provider alias for that is already declared in providers.tf.
    cloudfront_default_certificate = true
  }

  tags = { Name = local.name }
}
