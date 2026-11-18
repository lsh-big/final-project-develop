const token = location.hash.slice(1);
history.replaceState(null, '', location.pathname);
const form = document.querySelector('form');
const button = form.querySelector('button');
const status = document.querySelector('#status');
const inputs = form.querySelectorAll('input');
const selection = document.querySelector('#selection');
const selectionCount = document.querySelector('#selection-count');
const results = document.querySelector('#results');
const maxPhotos = 20;
const tokenPresent = /^[A-Za-z0-9_-]{43}$/.test(token);
const selected = [];

if (!tokenPresent) status.textContent = 'PC 화면의 QR을 스캔해 다시 열어 주세요.';

function renderSelection() {
  selection.replaceChildren();
  selection.hidden = selected.length === 0;
  selectionCount.textContent = selected.length ? `${selected.length}장 선택` : '선택한 사진이 없습니다.';
  button.disabled = selected.length === 0;
  for (const [index, file] of selected.entries()) {
    const row = document.createElement('li');
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '제외';
    remove.setAttribute('aria-label', `${file.name} 선택에서 제외`);
    remove.addEventListener('click', () => {
      selected.splice(index, 1);
      renderSelection();
    });
    row.append(file.name, remove);
    selection.append(row);
  }
}

for (const input of inputs) {
  input.disabled = !tokenPresent;
  input.addEventListener('change', () => {
    const files = Array.from(input.files ?? []);
    if (selected.length + files.length > maxPhotos) {
      status.textContent = `QR 한 번에 최대 ${maxPhotos}장까지 선택할 수 있습니다.`;
    } else if (files.length) {
      selected.push(...files);
      status.textContent = '';
      results.hidden = true;
      renderSelection();
    }
    input.value = '';
  });
}

async function uploadFile(file) {
  if (/^image\/(jpeg|png)$/.test(file.type) && /\.(jpe?g|png)$/i.test(file.name)
      && file.size <= 10 * 1024 * 1024) {
    const image = await decodeImage(file);
    if (image.width * image.height <= 24_000_000) return file;
    return toJpeg(image, file.name);
  }
  return toJpeg(await decodeImage(file), file.name);
}

async function decodeImage(file) {
  const url = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = url;
    await image.decode();
    return image;
  } catch {
    throw new Error('이 사진을 읽을 수 없습니다. 카메라로 다시 찍거나 JPG·PNG 사진을 선택해 주세요.');
  } finally {
    URL.revokeObjectURL(url);
  }
}

async function toJpeg(image, filename) {
  const scale = Math.min(1, 3000 / Math.max(image.width, image.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(image.width * scale));
  canvas.height = Math.max(1, Math.round(image.height * scale));
  const context = canvas.getContext('2d');
  context.fillStyle = '#fff';
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.88));
  if (!blob) throw new Error('사진을 변환할 수 없습니다. JPG·PNG 사진을 선택해 주세요.');
  return new File([blob], filename.replace(/\.[^.]+$/, '') + '.jpg', { type: 'image/jpeg' });
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!selected.length || button.disabled) return;
  for (const input of inputs) input.disabled = true;
  for (const remove of selection.querySelectorAll('button')) remove.disabled = true;
  button.disabled = true;
  results.replaceChildren();
  results.hidden = true;
  const items = [];
  let retry = true;
  try {
    const body = new FormData();
    for (const [index, photo] of selected.entries()) {
      status.textContent = `사진 준비 중… ${index + 1} / ${selected.length}`;
      try {
        const file = await uploadFile(photo);
        body.append('photos', file, file.name);
      } catch (error) {
        items.push({ filename: photo.name, error: error.message });
      }
    }
    if (body.has('photos')) {
      status.textContent = '전송 중입니다…';
      const response = await fetch('/upload-batch', {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + token },
        body,
      });
      const result = await response.json();
      if (!response.ok) {
        retry = ![404, 410].includes(response.status);
        throw new Error(typeof result.detail === 'string' ? result.detail : '전송하지 못했습니다.');
      }
      retry = !result.received;
      items.push(...result.items);
    }
    const failed = items.filter(({ error }) => error).length;
    const duplicate = items.filter((item) => item.duplicate).length;
    const sent = items.length - failed - duplicate;
    const counts = [sent && `${sent}장 전송 완료`, duplicate && `${duplicate}장 중복`, failed && `${failed}장 실패`].filter(Boolean);
    status.textContent = counts.join(' · ') + (failed && !retry ? '. 실패한 사진은 새 QR로 다시 전송해 주세요.' : '');
  } catch (error) {
    status.textContent = error instanceof TypeError ? 'PC에 연결할 수 없습니다. Wi-Fi 연결을 확인해 주세요.' : error.message;
  } finally {
    for (const item of items) {
      const row = document.createElement('li');
      row.textContent = `${item.filename} · ${item.error || (item.duplicate ? '이미 등록된 사진' : '전송 완료')}`;
      results.append(row);
    }
    results.hidden = items.length === 0;
    button.disabled = !retry;
    for (const input of inputs) input.disabled = !retry;
    for (const remove of selection.querySelectorAll('button')) remove.disabled = !retry;
  }
});
