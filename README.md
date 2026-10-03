# Digital twin từ dữ liệu DJI L2

Thư mục `digital-twin` là **ứng dụng độc lập**: viewer, thư viện JS/Python, shapefile, Digital Twin 3D và venv đều nằm trong đây. Không cần chạy hay import ứng dụng GIS gốc (`app.py`, `templates/`, `static/vendors` ngoài thư mục này).

## Viewer độc lập

```powershell
cd digital-twin
.\venv\Scripts\python.exe view-output.py
```

Hoặc từ bất kỳ thư mục nào:

```powershell
.\digital-twin\venv\Scripts\python.exe .\digital-twin\view-output.py
```

Cài lại Python packages (máy mới):

```powershell
.\digital-twin\venv\Scripts\python.exe -m pip install -r .\digital-twin\requirements.txt
```

---

Dựng digital twin photogrammetry từ ảnh UAV trong `LiDAR-image`, dùng thư viện `UAV-digital-twin` (Agisoft Metashape Professional).

File `.LDR` / `.LDRT` là LiDAR thô của DJI. Metashape không đọc được — không đưa vào pipeline. Muốn ghép LiDAR thì xuất LAS/LAZ bằng DJI Terra trước.

## Yêu cầu

- Windows, Python **3.11** (venv sẵn: `digital-twin\venv`)
- Agisoft **Metashape Professional** đã cài và **đã kích hoạt** (GUI 2.3.x ổn)
- Wheel Metashape trong `UAV-digital-twin\assets\` (đã cài vào venv)
- Ảnh `*_D.JPG` + file `.MRK` trong `LiDAR-image`

**VPS:** không gọi `metashape.exe` để xử lý (thiếu OpenGL). License GUI **không tự vào** wheel. Gắn license Pro vào venv rồi chạy module:

```powershell
# 1) Kiem tra (script tu tim file .lic trong thu muc cai Pro)
.\digital-twin\venv\Scripts\python.exe .\digital-twin\digital-twin-new.py --license-check

# 2a) Neu GUI da kich hoat tren VPS: giu bien moi truong (User) roi mo lai PowerShell
[System.Environment]::SetEnvironmentVariable(
  "agisoft_LICENSE",
  "C:\Program Files\Agisoft\Metashape Pro",
  "User"
)
# Bien phai la THU MUC, khong phai file metashape.lic

# 2b) Hoac gan key Pro mot lan vao module Python
.\digital-twin\venv\Scripts\python.exe .\digital-twin\digital-twin-new.py --activate XXXXX-XXXXX-XXXXX-XXXXX-XXXXX --license-check

# 3) Chay pipeline
.\digital-twin\venv\Scripts\python.exe .\digital-twin\digital-twin-new.py --cpu-only --quality medium
.\digital-twin\venv\Scripts\python.exe .\digital-twin\digital-twin-new.py --cpu-only --quality medium --mesh-3d
D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D --output D:\WORK\digital-twin\output-3d --quality medium --gpu-mask 10 --mesh-3d
D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D\DJI_202608211314_026_danphuong3D --output D:\WORK\digital-twin\output-3d-new --quality high --gpu-mask 10 --mesh-3d --texture-size 16384
D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D --output D:\WORK\digital-twin\output-3d --quality high --gpu-mask 10 --mesh-3d --texture-size 16384 --resume

D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D --output D:\WORK\digital-twin\output-3d --quality high --gpu-mask 10 --mesh-3d --texture-size 16384 --resume --rebuild-mesh

py -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D --output D:\WORK\digital-twin\output-3d --quality high --gpu-mask 10 --mesh-3d --texture-size 16384

D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\LiDAR-image --output D:\WORK\digital-twin\test --quality high --gpu-mask 10 --mesh-3d
.\venv\Scripts\python.exe -u digital-twin.py --images D:\WORK\digital-twin\data\LiDAR-image --output D:\WORK\digital-twin\test --quality high --gpu-mask 10 --mesh-3d --resume --rebuild-mesh

```

# 4) Chế độ chạy nhanh vẫn đảm bảo chất lượng:
D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D --output D:\WORK\digital-twin\output-3d-filnal --quality medium --depth-quality high --cloud-min-conf 2 --face-count max --mesh-3d --texture-size 16384 --texture-pages 4 --gpu-mask 10 

D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D --output D:\WORK\digital-twin\output-3d-filnal --quality medium --depth-quality high --cloud-min-conf 2 --face-count max --mesh-3d --texture-size 16384 --texture-pages 4 --gpu-mask 10 --resume

Hoặc:

D:\WORK\digital-twin\venv\Scripts\python.exe -u D:\WORK\digital-twin\digital-twin.py --images D:\WORK\digital-twin\data\danphuong3D --output D:\WORK\digital-twin\output-3d-filnal --quality medium --depth-quality ultra --cloud-min-conf 2 --face-count max --mesh-3d --texture-size 16384 --texture-pages 4 --gpu-mask 10

```

Cần **Metashape Professional**. Bản Standard không mở được module Python.

**Máy desktop** có GUI đã active, không muốn gắn wheel: thêm `--use-pro-exe`.

## Chạy

Mở PowerShell tại thư mục gốc repo `geotiff-viewer-final`:

```powershell
# Chỉ kiểm tra dữ liệu (không cần Metashape)
.\digital-twin\venv\Scripts\python.exe .\digital-twin\digital-twin-new.py --list-only

# Kiểm tra license
.\digital-twin\venv\Scripts\python.exe .\digital-twin\digital-twin.py --license-check

.\digital-twin\venv\Scripts\python.exe .\digital-twin\digital-twin.py --license-check
```

### GPU

`--gpu-mask` là bitmask: bit 0 = GPU đầu tiên, bit 1 = GPU thứ hai.

| Máy | Lệnh gợi ý |
|-----|------------|
| Chỉ NVIDIA | `--gpu-mask 1` |
| Intel HD + NVIDIA (Optimus) | `--gpu-mask 10` (chỉ NVIDIA, tắt Intel) |
| Intel HD + AMD | `--gpu-mask 10` (AMD = device 1) hoặc `--gpu-mask 1` nếu AMD là device 0 |
| Chỉ CPU | `--cpu-only` |


### Chất lượng

| `--quality` | Depth | Tốc độ | Khi nào dùng |
|-------------|-------|--------|----------------|
| `low` | ×8 | Nhanh | Máy yếu, xem trước |
| `medium` (mặc định) | ×4 | Trung bình | Twin thông thường |
| `high` | ×2 | Rất chậm | Máy GPU mạnh, RAM ≥ 32 GB |


## Pipeline

1. Đọc `*_D.JPG` + GPS từ `.MRK`
2. `addPhotos` → gắn tọa độ → lọc ảnh mờ
3. `matchPhotos` / `alignCameras` / `optimizeCameras`
4. `buildDepthMaps` → dense cloud → xuất LAS
5. Mesh + texture → OBJ (bỏ bằng `--skip-mesh`)
6. DEM + orthomosaic GeoTIFF (bỏ bằng `--skip-ortho`)

Bước lâu nhất: **depth map**. Trên CPU laptop có thể vài giờ; GPU NVIDIA giảm rõ.

## Kết quả (`digital-twin\output-new\`)

| File | Nội dung |
|------|----------|
| `digital_twin.psx` | Project Metashape |
| `point_cloud\point_cloud.las` | Mây điểm |
| `model\model.obj` | Mesh 3D |
| `dem\dem.tif` | DEM |
| `orthomosaic\orthomosaic.tif` | Orthomosaic |
| `report.pdf` | Báo cáo Metashape |
| `mission_catalog.json` | Danh sách ảnh + GPS |
| `camera_reference.csv` | GPS camera |
| `run.log` | Log UTF-8 của script (`[START]` / `[RUN]` / `[DONE]`). Console không in log Metashape. |
| `twin_summary.json` | Tóm tắt khi xong |

## Tham số chính

| Cờ | Ý nghĩa |
|----|---------|
| `--images` | Thư mục ảnh (mặc định `LiDAR-image`) |
| `--output` | Thư mục xuất (mặc định `output-new`) |
| `--quality low\|medium\|high` | Preset xử lý |
| `--gpu-mask` | Mask GPU, mặc định `10` |
| `--cpu-only` | Tắt GPU |
| `--list-only` | Chỉ liệt kê dữ liệu |
| `--activate KEY` | Gắn mã license Pro vào module Python (venv) |
| `--license-check` | Chỉ kiểm tra license |
| `--use-pro-exe` | Desktop: chạy qua `metashape.exe` (không dùng trên VPS) |
| `--no-relaunch` | Không gọi `metashape.exe` |
| `--reset` | Tạo `.psx` mới |
| `--skip-depth` | Bỏ estimating depth map |
| `--skip-mesh` | Không dựng mesh/texture |
| `--skip-ortho` | Không dựng DEM/ortho |
| `--skip-filter` | Không lọc ảnh mờ |
| `--monitor` | Ghi `monitor.csv` của UAV-digital-twin |

## Cài venv (máy mới)

```powershell
py -3.11 -m venv .\digital-twin\venv
.\digital-twin\venv\Scripts\python.exe -m pip install numpy scipy pillow psutil PyYAML
.\digital-twin\venv\Scripts\python.exe -m pip install .\digital-twin\UAV-digital-twin\assets\Metashape-2.2.1-cp37.cp38.cp39.cp310.cp311-none-win_amd64.whl
```

Cần Metashape **Professional** đã kích hoạt. Trên VPS gắn license vào venv (`--activate` hoặc `agisoft_LICENSE`), không chỉ mở GUI.
