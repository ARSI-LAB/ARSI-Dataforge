"""
Test module - DataSaverFactory class tests
"""
import pytest
import os
from types import SimpleNamespace

from base.dataset import MultimodalDataset

# Try to import ray
try:
    import ray
    HAS_RAY = True
except ImportError:
    ray = None
    HAS_RAY = False
    pytestmark = pytest.mark.skipif(not HAS_RAY, reason="Ray is not installed")

from curator.utils.io.data_saver_factory import DataSaverFactory


class TestDataSaverFactoryDetectFormat:
    """Test detect_format_from_path method"""

    def test_detect_json_format(self):
        """Test detecting JSON format"""
        assert DataSaverFactory.detect_format_from_path("data.json") == "json"
        assert DataSaverFactory.detect_format_from_path("output.jsonl") == "json"

    def test_detect_parquet_format(self):
        """Test detecting Parquet format"""
        assert DataSaverFactory.detect_format_from_path("data.parquet") == "parquet"

    def test_detect_csv_format(self):
        """Test detecting CSV format"""
        assert DataSaverFactory.detect_format_from_path("data.csv") == "csv"

    def test_detect_text_format(self):
        """Test detecting text format"""
        assert DataSaverFactory.detect_format_from_path("data.txt") == "text"

    def test_detect_default_format(self):
        """Test default format"""
        assert DataSaverFactory.detect_format_from_path("data.xyz") == "json"


class TestDataSaverFactorySaveDataset:
    """Test save_dataset method"""

    @pytest.mark.skip(reason="Ray+pandas compatibility issue")
    def test_save_as_json(self, tmp_path):
        """Test saving as JSON"""
        ray_ds = ray.data.from_items([{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
        output_path = str(tmp_path / "output.json")
        
        DataSaverFactory.save_dataset(ray_ds, output_path)
        
        assert os.path.exists(output_path)

    def test_save_as_csv(self, tmp_path):
        """Test saving as CSV"""
        ray_ds = ray.data.from_items([{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}])
        output_path = str(tmp_path / "output.csv")
        
        DataSaverFactory.save_dataset(ray_ds, output_path)
        
        assert os.path.exists(output_path)

    @pytest.mark.skip(reason="Ray+pandas compatibility issue")
    def test_save_creates_output_directory(self, tmp_path):
        """Test creating the output directory when saving"""
        ray_ds = ray.data.from_items([{"id": 1}])
        output_path = str(tmp_path / "subdir" / "output.json")
        
        DataSaverFactory.save_dataset(ray_ds, output_path)
        
        assert os.path.exists(output_path)


class TestDataSaverFactoryGetSupportedFormats:
    """Test get_supported_formats method"""

    def test_get_supported_formats(self):
        """Test getting supported formats"""
        formats = DataSaverFactory.get_supported_formats()
        
        assert isinstance(formats, list)
        assert "json" in formats
        assert "parquet" in formats
        assert "csv" in formats


class _FakeDataset:
    def __init__(self, rows, events=None):
        self.rows = rows
        self.events = events
        self.map_calls = 0
        self.written = False

    def schema(self):
        return SimpleNamespace(names=list(self.rows[0]))

    def map(self, func):
        self.map_calls += 1
        if self.events is not None:
            self.events.append("map")
        return _FakeDataset([func(dict(row)) for row in self.rows], self.events)

    def write_json(self, output_path):
        self.written = True

    def write_parquet(self, output_path):
        if self.events is not None:
            self.events.append("writer")
        self.written = True


def test_real_ray_save_strips_trajectory_audit_columns(tmp_path):
    source = ray.data.from_items([{
        "id": 1,
        "text": "preserved",
        "_trajectory_omissions": [{"op": "reasoning_strip"}],
        "_trajectory_degenerate_flags": {"repeated_action": {"max_streak": 6}},
        "_trajectory_normalized": True,
    }])
    output_path = str(tmp_path / "cleaned.parquet")

    DataSaverFactory.save_dataset(source, output_path, "parquet")

    assert ray.data.read_parquet(output_path).take_all() == [{"id": 1, "text": "preserved"}]
    assert "_trajectory_omissions" in source.schema().names


def test_save_without_audit_columns_does_not_add_map_stage():
    dataset = _FakeDataset([{"id": 1, "text": "preserved"}])

    DataSaverFactory.save_dataset(dataset, "unused.json", "json")

    assert dataset.map_calls == 0
    assert dataset.written is True


def test_audit_stripping_precedes_tensor_preparation_and_writer(monkeypatch):
    events = []
    dataset = _FakeDataset([{
        "id": 1, "_trajectory_omissions": [{"op": "reasoning_strip"}],
    }], events)

    def prepare(cls, current):
        events.append("tensor")
        assert current.schema().names == ["id"]
        return current

    monkeypatch.setattr(DataSaverFactory, "_prepare_tensor_columns", classmethod(prepare))
    DataSaverFactory.save_dataset(dataset, "unused.parquet", "parquet")

    assert events == ["map", "tensor", "writer"]
    assert dataset.rows[0]["_trajectory_omissions"] == [{"op": "reasoning_strip"}]


def test_schema_failure_does_not_reach_writer():
    class BrokenSchemaDataset(_FakeDataset):
        def schema(self):
            raise RuntimeError("schema unavailable")

    dataset = BrokenSchemaDataset([{"id": 1, "_trajectory_normalized": True}])
    with pytest.raises(RuntimeError, match="schema unavailable"):
        DataSaverFactory.save_dataset(dataset, "unused.json", "json")
    assert dataset.written is False


def test_saving_operation_returns_original_multimodal_dataset(monkeypatch):
    from curator.operations.saving.text_saving_operator import TextSavingOperation

    saved = []

    def save(dataset, output_path, format_type):
        saved.append((dataset, output_path, format_type))

    monkeypatch.setattr(DataSaverFactory, "save_dataset", save)
    dataset = MultimodalDataset(dataset=ray.data.from_items([{"id": 1}]))
    operation = TextSavingOperation({"output_path": "unused.json", "format": "json"})

    assert operation.execute(dataset) is dataset
    assert saved == [(dataset.dataset, "unused.json", "json")]
