from euqilegna_engine.models import RegionGeometry


def test_region_geometry_flags():
    region = RegionGeometry(
        region_id="region_1",
        color_id="1",
        area=100,
        path="M 0 0 L 10 0 L 10 10 Z",
        label_x=5,
        label_y=5,
        label_radius=3,
        palette_color="#ffffff",
        original_color="#eeeeee",
        paintability="comfortable",
    )
    assert region.is_labelable
    assert region.is_vectorized
