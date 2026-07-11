"""
AI domain fixtures for Marcus testing.

Provides real AI-related objects for testing AI components,
analysis engines, and enrichment systems without external API calls.
"""

import pytest

from src.ai.enrichment.intelligent_enricher import EnhancementResult, ProjectContext
from src.ai.providers.base_provider import EffortEstimate, SemanticAnalysis


@pytest.fixture
def sample_semantic_analysis():
    """Create a real semantic analysis result for testing."""
    return SemanticAnalysis(
        task_intent="Implement OAuth-based user authentication with JWT tokens",
        semantic_dependencies=["database schema", "encryption library"],
        risk_factors=["security vulnerabilities", "oauth configuration complexity"],
        suggestions=["add rate limiting", "add account lockout after failed attempts"],
        confidence=0.85,
        reasoning=(
            "Task explicitly names OAuth, JWT, and hashing — a security-sensitive "
            "authentication flow with a well-understood but non-trivial scope."
        ),
        risk_assessment={
            "security": "high",
            "complexity": "medium",
            "estimated_effort_hours": 8.0,
        },
    )


@pytest.fixture
def sample_effort_estimate():
    """Create a real effort estimate for testing."""
    return EffortEstimate(
        estimated_hours=12.5,
        confidence=0.8,
        factors=["oauth integration", "security requirements", "JWT token management"],
        similar_tasks=["task-001", "task-002"],
        risk_multiplier=1.2,
    )


@pytest.fixture
def sample_project_context(sample_task):
    """Create a real project context for AI operations."""
    return ProjectContext(
        project_type="web_application",
        tech_stack=["python", "fastapi", "postgresql", "react"],
        team_size=4,
        existing_tasks=[sample_task],
        project_standards={"code_style": "pep8", "test_coverage_min": 0.8},
        historical_data=[
            {"task_id": "task-000", "estimated_hours": 6.0, "actual_hours": 7.5},
        ],
        quality_requirements={"min_test_coverage": 0.8, "security_review_required": True},
    )


@pytest.fixture
def sample_enhancement_result(sample_task):
    """Create a real enhancement result for testing."""
    return EnhancementResult(
        original_task=sample_task,
        enhanced_description="Implement user authentication system with OAuth 2.0 support, including login, signup, password reset, and JWT token management",
        suggested_labels=["backend", "security", "authentication", "oauth"],
        estimated_hours=12.0,
        suggested_dependencies=["user database schema", "encryption library"],
        acceptance_criteria=[
            "Users can register with email and password",
            "OAuth login works with Google and GitHub",
            "JWT tokens are properly generated and validated",
            "Password reset functionality works",
            "All authentication endpoints are secured",
        ],
        risk_factors=["oauth configuration", "security implementation"],
        confidence=0.87,
        reasoning="High confidence given a well-scoped, precedented authentication flow.",
        changes_made={
            "description": "expanded with OAuth and JWT specifics",
            "labels": "added security, authentication, oauth",
        },
    )


@pytest.fixture
def ai_analysis_context():
    """Create context for AI analysis operations."""
    return {
        "project_domain": "web_development",
        "technical_stack": ["python", "javascript", "postgresql"],
        "team_expertise": ["backend", "frontend", "databases"],
        "project_complexity": "medium",
        "current_phase": "implementation",
        "available_resources": ["development team", "staging environment"],
        "constraints": ["2-week deadline", "security requirements"],
    }


@pytest.fixture
def enrichment_settings():
    """Create real enrichment settings for testing."""
    return {
        "enhancement_confidence_threshold": 0.7,
        "max_description_length": 500,
        "max_acceptance_criteria": 5,
        "enable_technical_analysis": True,
        "enable_risk_assessment": True,
        "enable_dependency_detection": True,
        "effort_estimation_model": "hybrid",
    }
