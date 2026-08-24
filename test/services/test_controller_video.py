import asyncio
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.controllers.manager.base_manager import TaskQueueFullError
from app.controllers.v1 import video as video_controller
from app.models import const
from app.models.exception import HttpException
from app.models.schema import TaskDeletionResponse, TaskListResponse, TaskQueryResponse
from app.services import material_upload
from app.services import state as sm
from app.utils import utils


class TestVideoControllerHelpers(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_sanitize_upload_filename_removes_client_path(self):
        """Sanitize upload filenames on Windows and POSIX."""
        for filename, expected in (
            (r"C:\videos\clip.MOV", "clip.MOV"),
            ("../../images/photo.png", "photo.png"),
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    video_controller._sanitize_upload_filename(filename, "request-123"),
                    expected,
                )

    def test_fastapi_startup_recovers_interrupted_cross_posts(self):
        """Recover interrupted publishing tasks on startup."""
        from app.services import task as task_service

        with patch.object(task_service, "recover_interrupted_cross_posts") as recover:

            async def run_lifespan():
                async with asgi.application_lifespan(asgi.app):
                    pass

            asyncio.run(run_lifespan())

        recover.assert_called_once_with()

    def test_sanitize_upload_filename_rejects_empty_name(self):
        """Reject empty filenames and directory traversal."""
        for filename in ("", ".", "..", "/"):
            with self.subTest(filename=filename):
                with self.assertRaises(HttpException) as raised:
                    video_controller._sanitize_upload_filename(filename, "request-123")
                self.assertEqual(raised.exception.status_code, 400)

    def test_resolve_path_maps_missing_and_unsafe_files(self):
        """ 404 403"""
        for error, expected_status in (
            ("file does not exist", 404),
            ("path escapes base directory", 403),
        ):
            with self.subTest(error=error):
                with patch.object(
                    video_controller.file_security,
                    "resolve_path_within_directory",
                    side_effect=ValueError(error),
                ):
                    with self.assertRaises(HttpException) as raised:
                        video_controller._resolve_path_within_directory(
                            "/tasks", "../secret", "request-123"
                        )
                self.assertEqual(raised.exception.status_code, expected_status)

    def test_parse_byte_range_supports_common_player_requests(self):
        cases = (
            (None, (0, 9)),
            ("bytes=2-5", (2, 5)),
            ("bytes=4-", (4, 9)),
            ("bytes=-4", (6, 9)),
            ("bytes=2-50", (2, 9)),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(
                    video_controller._parse_byte_range(header, 10, "request-123"),
                    expected,
                )

    def test_parse_byte_range_rejects_malformed_or_out_of_bounds_requests(self):
        """ Range  416 split  int  500"""
        invalid_headers = (
            "items=0-1",
            "bytes=",
            "bytes=10-",
            "bytes=5-2",
            "bytes=0-1,3-4",
        )
        for header in invalid_headers:
            with self.subTest(header=header):
                with self.assertRaises(HttpException) as raised:
                    video_controller._parse_byte_range(header, 10, "request-123")
                self.assertEqual(raised.exception.status_code, 416)


class TestVideoControllerTasks(unittest.TestCase):
    @staticmethod
    def _request():
        return SimpleNamespace(headers={"x-task-id": "request-123"})

    def test_create_task_queues_requested_pipeline_stage(self):
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task") as update_task,
            patch.object(video_controller.task_manager, "add_task") as add_task,
        ):
            response = video_controller.create_task(
                self._request(), body, stop_at="audio"
            )

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["data"]["task_id"], "task-123")
        self.assertEqual(response["data"]["request_id"], "request-123")
        update_task.assert_called_once_with("task-123")
        add_task.assert_called_once_with(
            video_controller.tm.start,
            task_id="task-123",
            params=body,
            stop_at="audio",
        )

    def test_create_task_removes_state_when_queue_is_full(self):
        """ 429"""
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm.state, "update_task"),
            patch.object(
                video_controller.task_manager,
                "add_task",
                side_effect=TaskQueueFullError("queue full"),
            ),
            patch.object(video_controller.sm.state, "delete_task") as delete_task,
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.create_task(self._request(), body, stop_at="video")

        self.assertEqual(raised.exception.status_code, 429)
        delete_task.assert_called_once_with("task-123")

    def test_create_task_removes_state_when_scheduler_fails(self):
        """ processing """
        body = MagicMock()
        body.model_dump.return_value = {"video_subject": "Coffee"}
        scheduling_error = RuntimeError("can't start new thread")
        state = sm.MemoryState()

        with (
            patch.object(video_controller.utils, "get_uuid", return_value="task-123"),
            patch.object(video_controller.sm, "state", state),
            patch.object(
                video_controller.task_manager,
                "add_task",
                side_effect=scheduling_error,
            ),
        ):
            with self.assertRaises(RuntimeError) as raised:
                video_controller.create_task(self._request(), body, stop_at="video")

        self.assertIs(raised.exception, scheduling_error)
        self.assertIsNone(state.get_task("task-123"))

    def test_get_all_tasks_preserves_pagination(self):
        with patch.object(
            video_controller.sm.state,
            "get_all_tasks",
            return_value=([{"id": "task-1", "cross_post_owner": "internal"}], 21),
        ) as get_all:
            response = video_controller.get_all_tasks(
                self._request(), page=2, page_size=10
            )

        self.assertEqual(
            response["data"],
            {
                "tasks": [{"id": "task-1"}],
                "total": 21,
                "page": 2,
                "page_size": 10,
            },
        )
        get_all.assert_called_once_with(2, 10)

    def test_task_query_returns_relative_url_without_mutating_state(self):
        """
        endpoint 未配置时应返回相对任务 URL，且不能把展示用 URL 回写到状态，
        否则后续请求可能基于已改写数据重复拼接路径。
        """
        task_id = "controller-task-url"
        task_dir = utils.task_dir(task_id)
        video_path = os.path.join(task_dir, "final-1.mp4")
        Path(video_path).write_bytes(b"fake-video")

        try:
            sm.state.update_task(
                task_id,
                state=const.TASK_STATE_COMPLETE,
                videos=[video_path],
                combined_videos=[video_path],
                cross_post_owner="localhost:123:internal",
            )
            with patch.dict(config.app, {"endpoint": ""}):
                response = video_controller.get_task(
                    self._request(), task_id=task_id, query=MagicMock()
                )

            self.assertEqual(
                response["data"]["videos"],
                [f"/tasks/{task_id}/final-1.mp4"],
            )
            self.assertNotIn("cross_post_owner", response["data"])
            self.assertIn("cross_post_owner", sm.state.get_task(task_id))
            self.assertEqual(sm.state.get_task(task_id)["videos"], [video_path])
        finally:
            sm.state.delete_task(task_id)
            shutil.rmtree(task_dir, ignore_errors=True)

    def test_task_query_preserves_structured_failure_details(self):
        failed_task = {
            "task_id": "failed-task",
            "state": const.TASK_STATE_FAILED,
            "progress": 30,
            "failed_stage": "audio",
            "error": "TTS request timed out",
        }

        with patch.object(
            video_controller.sm.state,
            "get_task",
            return_value=failed_task,
        ):
            response = video_controller.get_task(
                self._request(), task_id="failed-task", query=MagicMock()
            )

        self.assertEqual(response["data"], failed_task)

    def test_task_query_schema_documents_success_and_failure_states(self):
        """OpenAPI """
        examples = TaskQueryResponse.model_json_schema()["examples"]

        self.assertEqual(examples[0]["data"]["cross_post_state"], "complete")
        self.assertEqual(examples[1]["data"]["failed_stage"], "audio")
        self.assertTrue(examples[1]["data"]["error"])

        task_data_schema = TaskQueryResponse.model_json_schema()["$defs"][
            "TaskStatusData"
        ]
        self.assertIn("failed_stage", task_data_schema["properties"])
        self.assertIn("cross_post_state", task_data_schema["properties"])

        list_schema = TaskListResponse.model_json_schema()
        self.assertIn("TaskListData", list_schema["$defs"])
        self.assertIn("TaskStatusData", list_schema["$defs"])

    def test_task_deletion_schema_defines_null_data_contract(self):
        """TaskDeletionResponse  OpenAPI  data  null """
        schema = TaskDeletionResponse.model_json_schema()
        data_property = schema["properties"]["data"]

        self.assertEqual(data_property.get("type"), "null")
        self.assertIsNone(data_property.get("default"))

    def test_delete_rejects_generation_and_cross_posting_tasks(self):
        """ 409"""
        busy_tasks = (
            {
                "task_id": "generating-task",
                "state": const.TASK_STATE_PROCESSING,
                "progress": 30,
            },
            {
                "task_id": "publishing-task",
                "state": const.TASK_STATE_COMPLETE,
                "progress": 100,
                "cross_post_state": const.CROSS_POST_STATE_PROCESSING,
            },
        )

        for task in busy_tasks:
            with (
                self.subTest(task_id=task["task_id"]),
                patch.object(
                    video_controller.sm.state,
                    "get_task",
                    return_value=task,
                ),
                patch.object(video_controller.sm.state, "delete_task") as delete_task,
            ):
                with self.assertRaises(HttpException) as raised:
                    video_controller.delete_video(
                        self._request(), task_id=task["task_id"]
                    )

                self.assertEqual(raised.exception.status_code, 409)
                delete_task.assert_not_called()

    def test_delete_allows_completed_task(self):
        completed_task = {
            "task_id": "completed-task",
            "state": const.TASK_STATE_COMPLETE,
            "progress": 100,
            "cross_post_state": const.CROSS_POST_STATE_COMPLETE,
        }

        with (
            patch.object(
                video_controller.sm.state,
                "get_task",
                return_value=completed_task,
            ),
            patch.object(
                video_controller.utils,
                "task_dir",
                return_value="/tmp/mpt-completed-task-test",
            ),
            patch.object(video_controller.os.path, "exists", return_value=False),
            patch.object(video_controller.sm.state, "delete_task") as delete_task,
        ):
            response = video_controller.delete_video(
                self._request(), task_id="completed-task"
            )

        self.assertEqual(response["status"], 200)
        delete_task.assert_called_once_with("completed-task")

    def test_get_and_delete_missing_task_return_404(self):
        """ 404"""
        with patch.object(video_controller.sm.state, "get_task", return_value=None):
            for operation in (
                lambda: video_controller.get_task(
                    self._request(), task_id="missing", query=MagicMock()
                ),
                lambda: video_controller.delete_video(
                    self._request(), task_id="missing"
                ),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(HttpException) as raised:
                        operation()
                    self.assertEqual(raised.exception.status_code, 404)


class TestVideoControllerDeleteHTTP(unittest.TestCase):
    """DELETE /api/v1/tasks/{task_id}  HTTP """

    def setUp(self):
        self.original_app_config = dict(config.app)
        config.app["api_key"] = ""
        self.client = TestClient(asgi.app)

    def tearDown(self):
        config.app.clear()
        config.app.update(self.original_app_config)

    def _seed_completed_task(self, task_id: str) -> str:

        task_dir = utils.task_dir(task_id)
        video_path = os.path.join(task_dir, "final-1.mp4")
        Path(video_path).write_bytes(b"fake-video")
        sm.state.update_task(
            task_id,
            state=const.TASK_STATE_COMPLETE,
            progress=100,
            videos=[video_path],
            combined_videos=[video_path],
            cross_post_state=const.CROSS_POST_STATE_COMPLETE,
        )
        return task_dir

    def test_delete_completed_task_returns_success_response(self):
        """ 200,  (status/message/data"""

        task_id = "http-delete-success-task"
        task_dir = self._seed_completed_task(task_id)

        try:
            response = self.client.delete(f"/api/v1/tasks/{task_id}")
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)
            sm.state.delete_task(task_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": 200, "message": "success", "data": None},
        )

    def test_deleted_task_lookup_returns_404(self):
        """Verify deleted task returns 404 on subsequent lookups."""

        task_id = "http-delete-lookup-task"
        task_dir = self._seed_completed_task(task_id)

        try:
            delete_response = self.client.delete(f"/api/v1/tasks/{task_id}")
            self.assertEqual(delete_response.status_code, 200)

            lookup_response = self.client.get(f"/api/v1/tasks/{task_id}")
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)

        self.assertEqual(lookup_response.status_code, 404)


class TestVideoControllerFiles(unittest.TestCase):
    @staticmethod
    def _request(range_header=None):
        headers = {"x-task-id": "request-123"}
        if range_header is not None:
            headers["Range"] = range_header
        return SimpleNamespace(headers=headers)

    def test_upload_video_material_validates_complete_extension(self):
        upload = SimpleNamespace(
            filename=r"C:\videos\clip.MOV",
            file=BytesIO(b"video"),
        )
        with patch.object(
            material_upload,
            "save_material_upload",
            return_value="4fca18fce7344f3aa824777a40d45c8c.mov",
        ) as save_material:
            response = video_controller.upload_video_material_file(
                self._request(), upload
            )

        self.assertEqual(
            response["data"]["file"],
            "4fca18fce7344f3aa824777a40d45c8c.mov",
        )
        save_material.assert_called_once_with("clip.MOV", upload.file)

        invalid_upload = SimpleNamespace(
            filename="photojpg",
            file=BytesIO(b"not-an-image"),
        )
        with patch.object(
            material_upload,
            "save_material_upload",
            side_effect=material_upload.MaterialUploadError("unsupported format"),
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.upload_video_material_file(
                    self._request(), invalid_upload
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_upload_video_material_maps_service_failure_to_stable_500(self):
        upload = SimpleNamespace(filename="clip.mp4", file=BytesIO(b"video"))
        with patch.object(
            material_upload,
            "save_material_upload",
            side_effect=material_upload.MaterialServiceError(
                "C:\\sensitive\\storage is unavailable"
            ),
        ):
            with self.assertRaises(HttpException) as raised:
                video_controller.upload_video_material_file(self._request(), upload)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertNotIn("sensitive", raised.exception.message)

    def test_stream_video_returns_requested_bytes(self):
        """Range  Content-Range """

        async def consume(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "clip.mp4").write_bytes(b"0123456789")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.stream_video(
                        self._request("bytes=2-5"), "clip.mp4"
                    )
                )
                body = asyncio.run(consume(response))

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(response.headers["content-length"], "4")
        self.assertEqual(body, b"2345")

    def test_download_video_uses_resolved_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir, "final-1.mp4")
            video_path.write_bytes(b"video")
            with patch.object(
                video_controller.utils,
                "task_dir",
                return_value=temp_dir,
            ):
                response = asyncio.run(
                    video_controller.download_video(self._request(), "final-1.mp4")
                )

        # macOS  /var  /private/var
        self.assertEqual(response.path, os.path.realpath(video_path))
        self.assertEqual(response.filename, "final-1.mp4")
        self.assertEqual(response.media_type, "video/mp4")


if __name__ == "__main__":
    unittest.main()
