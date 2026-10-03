# DEM 3D

Ứng dụng độc lập xem GeoTIFF / DEM dạng lưới 3D (Three.js), tách từ phần **DEM 3D** của GeoTIFF Viewer.

## Chạy

```bat
run_server.bat
```

hoặc:

```bash
pip install -r requirements.txt
.\venv\Scripts\python.exe dem-3d\app.py
```

Mở trình duyệt: [http://127.0.0.1:5002](http://127.0.0.1:5002)

## Thành phần

| Phần | Vai trò |
|------|---------|
| `app.py` | Flask: upload/list GeoTIFF, API terrain + preview + nền OSM |
| `basemap.py` | Proxy / cache tile OpenStreetMap |
| `static/dem-3d.js` | Viewer mesh DEM, contour, chú giải cao độ |
| `static/app.js` | Danh sách file, tải lên, mở DEM |
| `data/` | File GeoTIFF / DEM đi kèm |

## Dữ liệu kèm theo

Các file DEM được lấy từ `projects/data` của ứng dụng gốc:

- `Na-San-DEM.tif`
- `dem-song-hong.tif`
- `dem15_18m1.tif`
- `Quang-Ngai-DEM.tif`
- `Na-San-3D.tif` (ảnh trực giao, vẫn xem được dạng bề mặt 3D)
- `Na-San-3D.contour.map.geojson`

Orthophoto rất lớn (An-Khanh, Thiên Nhân, …) và tileset Mesh 3D (`tdgb2`) **không** đưa vào đây vì thuộc phần khác.

File DEM lớn được liên kết cứng (hardlink) tới `projects/data` trên cùng ổ đĩa — không nhân đôi dung lượng. Không xóa các file đi kèm từ giao diện (tránh ảnh hưởng dữ liệu gốc).

Có thể thả thêm `.tif` / `.tiff` / `.vrt` vào `data/` rồi bấm **Danh sách GeoTIFF**, hoặc dùng **Tải lên dữ liệu**.

## Cổng / thư mục

| Biến môi trường | Mặc định | Ý nghĩa |
|-----------------|----------|---------|
| `DEM3D_HOST` | `127.0.0.1` | Host bind |
| `DEM3D_PORT` | `5002` | Cổng (tránh trùng app chính `5000`) |
| `DEM3D_DATA_DIR` | `./data` | Thư mục GeoTIFF |
