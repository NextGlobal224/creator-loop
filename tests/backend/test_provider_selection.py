import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.analysis.provider_selection import (
    provider_name,
    select_component_provider,
)
from creator_loop.backend.analysis.workers_creator import WorkersCreator
from creator_loop.shared.workflow import CreatorRequest, ResearchMaterial


class ProviderConfigurationTests(unittest.TestCase):
    def test_cloud_client_binds_annotation_to_exact_editing_stage(self):
        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "creator_loop.backend.analysis.workers_creator.create_workspace_marker",
                return_value={},
            ),
        ):
            root = Path(temp)
            (root / "secrets").mkdir()
            (root / "manifests").mkdir()
            (root / "runtime").mkdir()
            key = "a" * 64
            endpoint = "https://fixture.workers.dev/b/generate"
            version = "00000000-0000-4000-8000-000000000001"
            (root / "secrets/creator-cloud-key").write_text(key)
            (root / "secrets/creator-cloud-key.binding.json").write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                    }
                )
            )
            (root / "manifests/creator-cloud.json").write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "model": "@cf/openai/gpt-oss-120b",
                        "worker_version": version,
                        "key_file": "secrets/creator-cloud-key",
                    }
                )
            )
            provider = WorkersCreator(root)
            material = ResearchMaterial(
                "source:fixture",
                None,
                None,
                None,
                None,
                "TEXT",
                "Nguồn cần được đối chiếu.",
            )
            body = "Giả sử bạn nhận được một ảnh thiếu nguồn. " + " ".join(
                f"Gợi ý {n}: hãy đọc nguồn và xem căn cứ trước khi chia sẻ."
                for n in range(10)
            )
            content = {
                "observations": [
                    {
                        "material_id": material.material_id,
                        "quote": material.text,
                        "observation": "Ghi chú đề xuất đối chiếu.",
                    }
                ],
                "inferences": ["Giả thuyết biên tập."],
                "direction": "Hỏi nguồn gốc",
                "draft_body": body,
                "factual_claims": [],
            }
            stages = [
                {"stage": "analysis"},
                {"stage": "writing", "body": body},
                {"stage": "editing", "body": body},
                {"stage": "source_audit"},
            ]
            packet = {
                "model": provider.model.component_id,
                "worker_version": version,
                "result": {"response": json.dumps(content)},
                "stages": stages,
                "quality_audit": {
                    "finding_checks": [],
                    "draft_body": body,
                    "issues": [],
                    "paragraphs": [
                        {"text": body, "kind": "HYPOTHETICAL", "factual_passages": []}
                    ],
                },
            }
            request = CreatorRequest(
                "p",
                "AUTOMATIC",
                "Kiểm chứng",
                "Người đọc",
                "Rõ ràng",
                "TEXT",
                (material.material_id,),
            )

            def replies(final):
                identity = {
                    "model": provider.model.component_id,
                    "worker_version": version,
                }
                return [
                    json.dumps(
                        {
                            **identity,
                            "stage_result": {
                                "stage": stage,
                                "value": {"fixture": stage, "source_issues": []},
                                "usage": {},
                            },
                        }
                    ).encode()
                    for stage in ("analysis", "writing", "editing")
                ] + [json.dumps(final).encode()]

            with patch(
                "creator_loop.backend.analysis.workers_creator.post_owned",
                side_effect=replies(packet),
            ) as transport:
                self.assertEqual(
                    provider.generate(request, [material])["draft_body"], body
                )
                self.assertEqual(transport.call_count, 4)
                for index, call in enumerate(transport.call_args_list):
                    uploaded = json.loads(call.args[3])
                    self.assertEqual(
                        uploaded["stage"],
                        ["analysis", "writing", "editing", "source_audit"][index],
                    )
                    self.assertEqual(len(uploaded["previous"]), index)
            for invalid in (
                [{"stage": "editing", "body": body}],
                [stages[0], {"stage": "writing", "body": body + " changed"}, stages[2]],
            ):
                with (
                    self.subTest(stages=invalid),
                    patch(
                        "creator_loop.backend.analysis.workers_creator.post_owned",
                        side_effect=replies({**packet, "stages": invalid}),
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, "exact edited article"):
                        provider.generate(request, [material])
            with patch(
                "creator_loop.backend.analysis.workers_creator.post_owned",
                return_value=json.dumps(
                    {"model": "wrong", "worker_version": version}
                ).encode(),
            ) as transport:
                with self.assertRaisesRegex(ValueError, "identity mismatch"):
                    provider.generate(request, [material])
                self.assertEqual(transport.call_count, 1)
            stopped = {"value": False}
            first_reply = replies(packet)[0]

            def finish_then_cancel(*args):
                stopped["value"] = True
                return first_reply

            with patch(
                "creator_loop.backend.analysis.workers_creator.post_owned",
                side_effect=finish_then_cancel,
            ) as transport:
                with self.assertRaises(InterruptedError):
                    provider.generate(request, [material], lambda: stopped["value"])
                self.assertEqual(transport.call_count, 1)

    def test_explicit_cloud_config_is_idle_and_keeps_previous_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "secrets").mkdir()
            key = "a" * 64
            endpoint = "https://fixture.workers.dev/b/generate"
            (root / "secrets/creator-cloud-key").write_text(key)
            (root / "secrets/creator-cloud-key.binding.json").write_text(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                    }
                )
            )
            selected = root / "selected.json"
            config = {
                "endpoint": endpoint,
                "model": "@cf/openai/gpt-oss-120b",
                "worker_version": "00000000-0000-4000-8000-000000000001",
                "key_file": "secrets/creator-cloud-key",
            }
            selected.write_text(json.dumps(config))
            with patch(
                "creator_loop.backend.analysis.workers_creator.WorkersCreator.generate",
                side_effect=AssertionError("must remain idle"),
            ):
                self.assertEqual(
                    select_component_provider(root, selected), "CLOUDFLARE_WORKERS_AI"
                )
                self.assertEqual(provider_name(root), "CLOUDFLARE_WORKERS_AI")
                select_component_provider(root, selected)
            self.assertEqual(
                len(list((root / "manifests").glob("creator-cloud-previous-*.json"))), 1
            )
            sea = "@cf/aisingapore/gemma-sea-lion-v4-27b-it"
            selected.write_text(json.dumps({**config, "model": sea}))
            alternative = WorkersCreator(root, selected)
            self.assertEqual(alternative.model.component_id, sea)
            qwen = "@cf/qwen/qwen3-30b-a3b-fp8"
            selected.write_text(json.dumps({**config, "model": qwen}))
            self.assertEqual(WorkersCreator(root, selected).model.component_id, qwen)
            self.assertIn(sea, alternative.version)
            # Constructing a different explicit config never changes selection
            # or silently falls back when a generation fails.
            self.assertEqual(
                json.loads((root / "manifests/creator-cloud.json").read_text())[
                    "model"
                ],
                config["model"],
            )
            selected.write_text(json.dumps({**config, "model": "unverified-model"}))
            with self.assertRaisesRegex(ValueError, "Unknown cloud"):
                WorkersCreator(root, selected)
            selected.write_text(
                json.dumps(
                    {**config, "endpoint": "https://different.workers.dev/b/generate"}
                )
            )
            with self.assertRaisesRegex(ValueError, "different Worker"):
                select_component_provider(root, selected)
            self.assertEqual(
                json.loads((root / "manifests/creator-cloud.json").read_text())[
                    "endpoint"
                ],
                endpoint,
            )
