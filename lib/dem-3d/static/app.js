/**
 * Điều khiển ứng dụng DEM 3D độc lập: danh sách / upload / mở terrain.
 */
(function (global) {
  const openEntries = [];
  const listEl = document.getElementById('geotiffList');
  const hintEl = document.getElementById('dem3dHint');
  const browseBtn = document.getElementById('browseButton');
  const fileInput = document.getElementById('brownFileInput');
  const progressBarEl = document.getElementById('uploadProgressBar');
  const progressTextEl = document.getElementById('uploadProgressText');
  const exag = document.getElementById('demExaggeration');
  const contourToggle = document.getElementById('demContourToggle');

  function setHint(text) {
    if (hintEl) hintEl.textContent = text || '';
  }

  function getOpenEntries() {
    return openEntries.slice();
  }

  function setActiveFile(fileId, path, filename) {
    openEntries.length = 0;
    if (fileId) {
      openEntries.push({ fileId: fileId, path: path, filename: filename });
    }
    document.querySelectorAll('.geotiff-list-item').forEach(function (row) {
      row.classList.toggle('is-active', row.dataset.fileId === fileId);
    });
  }

  function formatSize(bytes) {
    const n = Number(bytes) || 0;
    if (n >= 1024 * 1024 * 1024) return (n / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
    if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
    return n + ' B';
  }

  function renderList(files) {
    listEl.innerHTML = '';
    files.forEach(function (item) {
      const row = document.createElement('div');
      row.className = 'geotiff-list-item';
      row.dataset.path = item.path;
      row.dataset.fileId = item.file_id || '';
      const name = document.createElement('span');
      name.className = 'geotiff-file-name';
      name.title = item.path + (item.size ? ' (' + formatSize(item.size) + ')' : '');
      name.textContent = item.filename || item.path;
      row.appendChild(name);
      const bundled = /^(na-san-dem|dem-song-hong|dem15_18m1|quang-ngai-dem|na-san-3d)\.(tif|tiff|vrt)$/i.test(item.filename || item.path || '');
      if (!bundled) {
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'geotiff-delete-btn';
        del.textContent = 'Xóa';
        del.addEventListener('click', function (ev) {
          ev.stopPropagation();
          deleteFile(item.path, row);
        });
        row.appendChild(del);
      }
      row.addEventListener('click', function () {
        openFile(item);
      });
      listEl.appendChild(row);
    });
    const active = openEntries[0];
    if (active) setActiveFile(active.fileId, active.path, active.filename);
  }

  async function fetchList() {
    const res = await fetch('/api/geotiff/list?refresh=1');
    const data = await res.json();
    if (!data.success) throw new Error(data.message || 'Không lấy được danh sách GeoTIFF');
    return data.files || [];
  }

  async function refreshList(show) {
    const files = await fetchList();
    renderList(files);
    if (show || files.length) listEl.hidden = false;
    if (!files.length) setHint('Chưa có GeoTIFF trong thư mục data/. Hãy tải file lên.');
    return files;
  }

  async function openFile(item) {
    const fileId = item.file_id || item.fileId;
    const path = item.path;
    const filename = item.filename || path;
    if (!fileId) {
      const res = await fetch('/api/geotiff/load-path', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: path })
      });
      const data = await res.json();
      if (!data.success) throw new Error(data.message || 'Không mở được GeoTIFF');
      return openFile(data);
    }
    setActiveFile(fileId, path, filename);
    setHint('Đang dựng DEM 3D: ' + filename);
    Dem3D.show3DMode(true);
    try {
      await Dem3D.loadDem3D(fileId, 256);
      Dem3D.setContoursVisible(!!(contourToggle && contourToggle.checked));
      setHint(filename);
    } catch (err) {
      console.error(err);
      setHint(err.message || 'Không mở được chế độ DEM 3D');
      alert(err.message || 'Không mở được chế độ DEM 3D');
    }
  }

  async function deleteFile(path, rowEl) {
    if (!window.confirm('Xóa file ' + path + '?')) return;
    const res = await fetch('/api/geotiff/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path })
    });
    const data = await res.json();
    if (!data.success) {
      alert(data.message || 'Không xóa được file');
      return;
    }
    if (openEntries[0] && openEntries[0].path === path) {
      openEntries.length = 0;
      Dem3D.destroyViewer();
    }
    if (rowEl) rowEl.remove();
    await refreshList(true);
  }

  function uploadOne(file, totalBytes, uploadedBytes) {
    return new Promise(function (resolve, reject) {
      const formData = new FormData();
      formData.append('geotiff', file);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/geotiff/upload-single', true);
      xhr.upload.onprogress = function (ev) {
        if (!ev.lengthComputable || !totalBytes) return;
        const pct = Math.max(0, Math.min(100, Math.round(((uploadedBytes + ev.loaded) / totalBytes) * 100)));
        if (progressBarEl) progressBarEl.style.width = pct + '%';
        if (progressTextEl) progressTextEl.textContent = pct + '%';
      };
      xhr.onload = function () {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch (_e) {
          reject(new Error('Không parse được phản hồi upload'));
        }
      };
      xhr.onerror = function () { reject(new Error('Lỗi mạng khi upload file')); };
      xhr.send(formData);
    });
  }

  browseBtn.addEventListener('click', async function () {
    if (!listEl.hidden && listEl.children.length) {
      listEl.hidden = true;
      return;
    }
    try {
      await refreshList(true);
    } catch (err) {
      setHint(err.message || 'Lỗi khi lấy danh sách GeoTIFF');
    }
  });

  fileInput.addEventListener('change', async function (event) {
    const files = event.target.files ? Array.from(event.target.files) : [];
    event.target.value = '';
    const geotiffs = files.filter(function (f) {
      const name = (f.name || '').toLowerCase();
      return name.endsWith('.tif') || name.endsWith('.tiff') || name.endsWith('.vrt');
    });
    if (!geotiffs.length) {
      setHint('Không tìm thấy GeoTIFF (.tif/.tiff/.vrt).');
      return;
    }
    const totalBytes = geotiffs.reduce(function (sum, file) { return sum + (file.size || 0); }, 0);
    let uploadedBytes = 0;
    let lastOk = null;
    if (progressBarEl) progressBarEl.style.width = '0%';
    if (progressTextEl) progressTextEl.textContent = '0%';
    for (let i = 0; i < geotiffs.length; i++) {
      try {
        const data = await uploadOne(geotiffs[i], totalBytes, uploadedBytes);
        uploadedBytes += geotiffs[i].size || 0;
        if (data && data.success) lastOk = data;
        else setHint((data && data.message) || 'Upload thất bại');
      } catch (err) {
        uploadedBytes += geotiffs[i].size || 0;
        setHint(err.message || 'Upload thất bại');
      }
    }
    if (progressBarEl) progressBarEl.style.width = '100%';
    if (progressTextEl) progressTextEl.textContent = '100%';
    const filesNow = await refreshList(true);
    if (lastOk) {
      await openFile(lastOk);
    } else if (filesNow.length) {
      setHint('Một số file đã tồn tại. Chọn trong danh sách để mở.');
    }
  });

  if (exag) {
    exag.addEventListener('input', function () {
      Dem3D.setExaggeration(exag.value);
    });
  }
  if (contourToggle) {
    contourToggle.addEventListener('change', function () {
      Dem3D.setContoursVisible(!!contourToggle.checked);
    });
  }

  global.getOpenGeotiffEntries = getOpenEntries;
  global.getOpenEntries = getOpenEntries;

  (async function boot() {
    Dem3D.show3DMode(true);
    try {
      const files = await refreshList(true);
      if (!files.length) return;
      const preferred = files.find(function (f) {
        return /dem/i.test(f.filename || f.path || '');
      }) || files[0];
      await openFile(preferred);
    } catch (err) {
      setHint(err.message || 'Không khởi tạo được DEM 3D');
    }
  })();
})(window);
