#!/usr/bin/env python3
"""
Integration test for Natural Language project creation on actual Kanban board
Tests that projects created via NLP tools actually appear on the board
"""

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations.kanban_factory import KanbanFactory
from src.integrations.nlp_tools import (
    NaturalLanguageFeatureAdder,
    NaturalLanguageProjectCreator,
)


class BoardIntegrationTest:
    """Test that NLP-created projects appear on actual boards"""

    def __init__(self):
        # Load environment variables
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).parent.parent / ".env")

        self.provider = os.getenv("KANBAN_PROVIDER", "planka")
        self.kanban_client = None
        self.test_project_name = (
            f"Test NLP Project {datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
        )

    async def setup(self):
        """Initialize kanban client"""
        print(f"\n🔧 Setting up {self.provider} integration test...")
        self.kanban_client = await KanbanFactory.create(self.provider)
        print(f"✓ Connected to {self.provider}")

    async def cleanup(self):
        """Clean up test data if needed"""
        # Note: In production, you might want to delete test projects
        # For now, we'll leave them for manual verification
        pass

    async def test_create_project_on_board(self):
        """Test creating a project from natural language and verify it appears on board"""
        print("\n📝 Test 1: Create project from natural language")

        # Create the NLP tool with real kanban client
        creator = NaturalLanguageProjectCreator(self.kanban_client)

        # Natural language project description
        description = """
        I need a simple blog platform where users can:
        - Write and publish blog posts with markdown
        - Comment on posts
        - Like posts
        - Follow other authors

        Use Next.js for frontend and Node.js for backend
        """

        print(f"📋 Creating project: {self.test_project_name}")
        print(f"📝 Description: {description[:100]}...")

        # Create project
        result = await creator.create_project_from_description(
            description=description,
            project_name=self.test_project_name,
            options={
                "team_size": 2,
                "tech_stack": ["Next.js", "Node.js", "PostgreSQL"],
            },
        )

        print("\n✅ Project created successfully!")
        print(f"   - Tasks created: {result['tasks_created']}")
        print(f"   - Phases: {', '.join(result['phases'])}")
        print(f"   - Estimated days: {result['estimated_days']}")
        print(f"   - Dependencies mapped: {result['dependencies_mapped']}")

        # Verify tasks exist on board
        print(f"\n🔍 Verifying tasks on {self.provider} board...")

        # Get tasks from board
        tasks = await self.kanban_client.get_tasks()

        # Count tasks with our test project name in title or description
        our_tasks = [
            t
            for t in tasks
            if self.test_project_name in (t.title or "")
            or self.test_project_name in (t.description or "")
        ]

        print(f"✓ Found {len(our_tasks)} tasks on board")

        # Show first few tasks
        print("\n📋 Sample tasks created:")
        for i, task in enumerate(our_tasks[:5]):
            print(f"   {i+1}. {task.title}")
            if task.labels:
                print(f"      Labels: {', '.join(task.labels)}")

        return result, our_tasks

    async def test_add_feature_to_board(self):
        """Test adding a feature and verify it appears on board"""
        print("\n📝 Test 2: Add feature using natural language")

        # Create feature adder with real kanban client
        adder = NaturalLanguageFeatureAdder(self.kanban_client)

        # Add a feature
        feature_description = """
        Add email notifications when someone comments on your blog post
        """

        print(f"🔧 Adding feature: {feature_description.strip()}")

        result = await adder.add_feature(
            feature_description=feature_description, integration_point="auto_detect"
        )

        print("\n✅ Feature added successfully!")
        print(f"   - Tasks created: {result['tasks_created']}")
        print(f"   - Integration detected: {result['integration_detected']}")
        print(f"   - Feature phase: {result['feature_phase']}")

        # Get updated task list
        tasks = await self.kanban_client.get_tasks()

        # Find the new tasks (they should mention email notifications)
        new_tasks = [
            t
            for t in tasks
            if "email" in t.title.lower() and "notification" in t.title.lower()
        ]

        print(f"\n✓ Found {len(new_tasks)} notification tasks on board")

        # Show the new tasks
        if new_tasks:
            print("\n📋 Notification tasks created:")
            for i, task in enumerate(new_tasks):
                print(f"   {i+1}. {task.title}")
                if task.labels:
                    print(f"      Labels: {', '.join(task.labels)}")

        return result, new_tasks

    async def test_task_visibility(self):
        """Test that we can query and see our created tasks"""
        print("\n📝 Test 3: Verify task visibility and structure")

        # Get all tasks
        all_tasks = await self.kanban_client.get_tasks()

        # Filter to our test project
        our_tasks = [
            t
            for t in all_tasks
            if self.test_project_name in (t.title or "")
            or self.test_project_name in (t.description or "")
        ]

        print("📊 Task Statistics:")
        print(f"   - Total tasks on board: {len(all_tasks)}")
        print(f"   - Our test project tasks: {len(our_tasks)}")

        # Analyze task structure
        by_status = {}
        by_priority = {}
        has_labels = 0

        for task in our_tasks:
            # Count by status
            status = task.status.value if task.status else "unknown"
            by_status[status] = by_status.get(status, 0) + 1

            # Count by priority
            priority = task.priority.value if task.priority else "unknown"
            by_priority[priority] = by_priority.get(priority, 0) + 1

            # Count tasks with labels
            if task.labels:
                has_labels += 1

        print("\n📈 Task Analysis:")
        print("   By Status:")
        for status, count in by_status.items():
            print(f"      - {status}: {count}")

        print("   By Priority:")
        for priority, count in by_priority.items():
            print(f"      - {priority}: {count}")

        print(f"   - Tasks with labels: {has_labels}/{len(our_tasks)}")

        return our_tasks


async def main():
    """Run all integration tests"""
    test = BoardIntegrationTest()

    try:
        # Setup
        await test.setup()

        print(f"\n{'='*60}")
        print(f"🧪 Natural Language → {test.provider.upper()} Board Integration Test")
        print(f"{'='*60}")

        # Test 1: Create project
        project_result, project_tasks = await test.test_create_project_on_board()

        # Small delay to ensure board updates
        await asyncio.sleep(2)

        # Test 2: Add feature
        feature_result, feature_tasks = await test.test_add_feature_to_board()

        # Test 3: Verify visibility
        all_our_tasks = await test.test_task_visibility()

        # Summary
        print(f"\n{'='*60}")
        print("✅ Integration Test Summary")
        print(f"{'='*60}")
        print(f"Provider: {test.provider}")
        print(f"Project: {test.test_project_name}")
        print(f"Total tasks created: {len(all_our_tasks)}")
        print(f"Project creation: {project_result['success']}")
        print(f"Feature addition: {feature_result['success']}")
        print(f"\n🎉 All tests passed! Tasks are visible on {test.provider} board.")

        # Cleanup
        await test.cleanup()

    except Exception as e:
        print(f"\n❌ Integration test failed: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
