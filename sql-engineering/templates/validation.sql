/* @VALIDATION_HEADER
header_version: 1
spec_path: validations/<artifact-slug>/vNNN.spec.json
artifact: VALIDATION/<artifact-slug>/vNNN
title: <验证标题>
purpose: <验证要锁定的粒度、指标和证据>
evidence_status: missing | passed | skipped | proxy_verified
promotion_decision: blocked | query_more | promote_to_dashboard | promote_unverified_dashboard | promote_proxy_verified_dashboard
confidence_score: 0.00
@END_VALIDATION_HEADER */

-- Validation artifacts may be evidence-only through vNNN.spec.json plus run evidence.
-- Add executable validation SQL here only when a concrete check query is needed.
