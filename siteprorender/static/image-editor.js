/*
 * Editor de imagem com caixa de corte retangular padrão.
 *
 * Anexa-se a qualquer <input type="file" class="perspective-upload">.
 * Ao selecionar uma imagem, abre um editor com uma caixa de corte
 * retangular sobre a imagem: arrastando os cantos, redimensiona a
 * largura e a altura ao mesmo tempo; arrastando uma borda (topo,
 * base, esquerda ou direita), redimensiona só naquela direção;
 * arrastando o miolo da caixa, ela é movida. Um controle de zoom
 * ajusta o enquadramento da imagem por trás da caixa. Ao aplicar, a
 * imagem final (já recortada) substitui o arquivo original no
 * próprio <input>, então nenhuma mudança é necessária no backend —
 * o formulário envia normalmente.
 */
(function () {
    'use strict';

    // Tamanho (em px CSS) da área de edição quadrada mostrada na tela.
    // Todas as coordenadas de interação (caixa de corte, ponteiro etc.)
    // são tratadas neste mesmo espaço, independente do zoom do navegador
    // ou do tamanho real da imagem.
    const STAGE_SIZE = 420;
    const OUTPUT_SIZE = 1920;
    const MIN_BOX = 32;

    let modalEl = null;

    function clamp(v, min, max) {
        return Math.max(min, Math.min(max, v));
    }

    function buildModal() {
        const overlay = document.createElement('div');
        overlay.className = 'pe-overlay';
        overlay.innerHTML =
            '<div class="pe-box">' +
                '<button type="button" class="big-alert-close pe-cancel" aria-label="Fechar"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><title>x</title><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7L17 17M17 7L7 17"/></svg></button>' +
                '<div class="pe-title">Ajustar Imagem</div>' +
                '<div class="pe-hint">Arraste os cantos para redimensionar a caixa, as bordas para ajustar só um lado, ou o miolo para mover. Use o zoom para enquadrar a imagem.</div>' +
                '<div class="pe-canvas-wrap">' +
                    '<div class="pe-stage">' +
                        '<img class="pe-stage-img" draggable="false" alt="">' +
                        '<div class="pe-crop-box">' +
                            '<div class="pe-crop-grid"></div>' +
                            '<div class="pe-crop-handle pe-h-n" data-dir="n"></div>' +
                            '<div class="pe-crop-handle pe-h-s" data-dir="s"></div>' +
                            '<div class="pe-crop-handle pe-h-e" data-dir="e"></div>' +
                            '<div class="pe-crop-handle pe-h-w" data-dir="w"></div>' +
                            '<div class="pe-crop-handle pe-h-nw" data-dir="nw"></div>' +
                            '<div class="pe-crop-handle pe-h-ne" data-dir="ne"></div>' +
                            '<div class="pe-crop-handle pe-h-sw" data-dir="sw"></div>' +
                            '<div class="pe-crop-handle pe-h-se" data-dir="se"></div>' +
                        '</div>' +
                    '</div>' +
                '</div>' +
                '<div class="pe-controls">' +
                    '<label>Zoom <input type="range" class="pe-zoom" min="50" max="300" value="100"></label>' +
                    '<button type="button" class="pe-reset btn-sm">Redefinir</button>' +
                '</div>' +
                '<div class="pe-actions">' +
                    '<button type="button" class="pe-apply btn-sm">Aplicar</button>' +
                '</div>' +
            '</div>';
        document.body.appendChild(overlay);
        return overlay;
    }

    // Recorta [sx,sy,sw,sh] (em pixels naturais da imagem, podendo
    // extrapolar as bordas) para dentro de [outW,outH], deixando
    // transparente a parte que ficar fora dos limites da imagem.
    function drawClippedCrop(ctx, img, sx, sy, sw, sh, outW, outH) {
        const nw = img.naturalWidth, nh = img.naturalHeight;
        const scaleX = outW / sw;
        const scaleY = outH / sh;
        let dx = 0, dy = 0, dw = outW, dh = outH;

        if (sx < 0) {
            const cut = -sx;
            sx = 0; sw -= cut;
            dx += cut * scaleX; dw -= cut * scaleX;
        }
        if (sy < 0) {
            const cut = -sy;
            sy = 0; sh -= cut;
            dy += cut * scaleY; dh -= cut * scaleY;
        }
        if (sx + sw > nw) {
            const cut = (sx + sw) - nw;
            sw -= cut; dw -= cut * scaleX;
        }
        if (sy + sh > nh) {
            const cut = (sy + sh) - nh;
            sh -= cut; dh -= cut * scaleY;
        }

        if (sw > 0.5 && sh > 0.5 && dw > 0.5 && dh > 0.5) {
            ctx.drawImage(img, sx, sy, sw, sh, dx, dy, dw, dh);
        }
    }

    function openEditor(file, onApply) {
        if (!modalEl) modalEl = buildModal();
        modalEl.style.display = 'flex';

        const stage = modalEl.querySelector('.pe-stage');
        const imgEl = modalEl.querySelector('.pe-stage-img');
        const cropBox = modalEl.querySelector('.pe-crop-box');
        const handles = Array.from(modalEl.querySelectorAll('.pe-crop-handle'));
        const zoomSlider = modalEl.querySelector('.pe-zoom');
        const resetBtn = modalEl.querySelector('.pe-reset');
        const cancelBtn = modalEl.querySelector('.pe-cancel');
        const applyBtn = modalEl.querySelector('.pe-apply');

        const img = new Image();
        const objectUrl = URL.createObjectURL(file);

        let baseScale = 1;
        let zoom = 1;
        // Caixa de corte, sempre em coordenadas do "palco" (0..STAGE_SIZE).
        let crop = { x: 0, y: 0, w: STAGE_SIZE, h: STAGE_SIZE };
        let drag = null; // { mode: 'move'|'resize', dir, startCrop, startPoint }

        function imageRect() {
            const w = img.naturalWidth * baseScale * zoom;
            const h = img.naturalHeight * baseScale * zoom;
            return {
                x: STAGE_SIZE / 2 - w / 2,
                y: STAGE_SIZE / 2 - h / 2,
                w: w, h: h
            };
        }

        function updateImageStyle() {
            const r = imageRect();
            imgEl.style.width = r.w + 'px';
            imgEl.style.height = r.h + 'px';
            imgEl.style.left = r.x + 'px';
            imgEl.style.top = r.y + 'px';
        }

        function renderCropBox() {
            cropBox.style.left = crop.x + 'px';
            cropBox.style.top = crop.y + 'px';
            cropBox.style.width = crop.w + 'px';
            cropBox.style.height = crop.h + 'px';
        }

        function resetCrop() {
            // Por padrão a caixa começa cobrindo a imagem inteira (sem
            // recorte); o usuário estreita a partir daí.
            const r = imageRect();
            crop = {
                x: clamp(r.x, 0, STAGE_SIZE),
                y: clamp(r.y, 0, STAGE_SIZE),
                w: clamp(r.w, MIN_BOX, STAGE_SIZE),
                h: clamp(r.h, MIN_BOX, STAGE_SIZE)
            };
            // Se a imagem for maior que o palco, a caixa cobre o palco todo.
            if (r.x <= 0 && r.y <= 0 && r.w >= STAGE_SIZE && r.h >= STAGE_SIZE) {
                crop = { x: 0, y: 0, w: STAGE_SIZE, h: STAGE_SIZE };
            }
            renderCropBox();
        }

        // Redimensiona a caixa `start` a partir da direção `dir`
        // (combinação de 'n'/'s'/'e'/'w'), deslocando em (dx, dy) — sempre
        // limitada aos limites do palco e a um tamanho mínimo.
        function resizeBox(dir, dx, dy, start) {
            let { x, y, w, h } = start;

            if (dir.indexOf('n') !== -1) {
                const newY = clamp(start.y + dy, 0, start.y + start.h - MIN_BOX);
                h = start.h + (start.y - newY);
                y = newY;
            }
            if (dir.indexOf('s') !== -1) {
                h = clamp(start.h + dy, MIN_BOX, STAGE_SIZE - start.y);
            }
            if (dir.indexOf('w') !== -1) {
                const newX = clamp(start.x + dx, 0, start.x + start.w - MIN_BOX);
                w = start.w + (start.x - newX);
                x = newX;
            }
            if (dir.indexOf('e') !== -1) {
                w = clamp(start.w + dx, MIN_BOX, STAGE_SIZE - start.x);
            }
            return { x, y, w, h };
        }

        function stagePoint(evt) {
            const rect = stage.getBoundingClientRect();
            const scale = STAGE_SIZE / rect.width;
            return {
                x: (evt.clientX - rect.left) * scale,
                y: (evt.clientY - rect.top) * scale
            };
        }

        function onPointerDown(mode, dir) {
            return function (evt) {
                evt.preventDefault();
                evt.stopPropagation();
                drag = {
                    mode: mode,
                    dir: dir,
                    startPoint: stagePoint(evt),
                    startCrop: { x: crop.x, y: crop.y, w: crop.w, h: crop.h }
                };
                if (evt.pointerId !== undefined && evt.target.setPointerCapture) {
                    try { evt.target.setPointerCapture(evt.pointerId); } catch (e) { /* ignora */ }
                }
            };
        }

        function onPointerMove(evt) {
            if (!drag) return;
            evt.preventDefault();
            const p = stagePoint(evt);
            const dx = p.x - drag.startPoint.x;
            const dy = p.y - drag.startPoint.y;

            if (drag.mode === 'move') {
                crop.x = clamp(drag.startCrop.x + dx, 0, STAGE_SIZE - drag.startCrop.w);
                crop.y = clamp(drag.startCrop.y + dy, 0, STAGE_SIZE - drag.startCrop.h);
            } else {
                crop = resizeBox(drag.dir, dx, dy, drag.startCrop);
            }
            renderCropBox();
        }

        function onPointerUp() {
            drag = null;
        }

        const onCropBoxDown = onPointerDown('move', null);
        const onHandleDown = handles.map(function (h) {
            return onPointerDown('resize', h.dataset.dir);
        });

        cropBox.addEventListener('pointerdown', onCropBoxDown);
        handles.forEach(function (h, i) {
            h.addEventListener('pointerdown', onHandleDown[i]);
        });
        window.addEventListener('pointermove', onPointerMove);
        window.addEventListener('pointerup', onPointerUp);
        window.addEventListener('pointercancel', onPointerUp);

        zoomSlider.value = 100;
        zoomSlider.oninput = function () {
            zoom = zoomSlider.value / 100;
            updateImageStyle();
        };

        resetBtn.onclick = function () {
            zoom = 1;
            zoomSlider.value = 100;
            updateImageStyle();
            resetCrop();
        };

        function cleanup() {
            modalEl.style.display = 'none';
            cropBox.removeEventListener('pointerdown', onCropBoxDown);
            handles.forEach(function (h, i) {
                h.removeEventListener('pointerdown', onHandleDown[i]);
            });
            window.removeEventListener('pointermove', onPointerMove);
            window.removeEventListener('pointerup', onPointerUp);
            window.removeEventListener('pointercancel', onPointerUp);
            URL.revokeObjectURL(objectUrl);
        }

        cancelBtn.onclick = function () {
            cleanup();
        };

        applyBtn.onclick = function () {
            const r = imageRect();
            const factor = baseScale * zoom;

            // Caixa de corte convertida para pixels naturais da imagem.
            const sx = (crop.x - r.x) / factor;
            const sy = (crop.y - r.y) / factor;
            const sw = crop.w / factor;
            const sh = crop.h / factor;

            // Tamanho de saída: mantém a proporção da caixa desenhada na
            // tela, normalizada para no máximo OUTPUT_SIZE de lado.
            const outScale = OUTPUT_SIZE / Math.max(crop.w, crop.h);
            const outW = Math.max(1, Math.round(crop.w * outScale));
            const outH = Math.max(1, Math.round(crop.h * outScale));

            const outCanvas = document.createElement('canvas');
            outCanvas.width = outW;
            outCanvas.height = outH;
            const ctx = outCanvas.getContext('2d');
            drawClippedCrop(ctx, img, sx, sy, sw, sh, outW, outH);

            exportCanvas(outCanvas, function (blob) {
                if (blob) onApply(blob);
                cleanup();
            });
        };

        img.onload = function () {
            baseScale = Math.min(
                (STAGE_SIZE * 0.85) / img.naturalWidth,
                (STAGE_SIZE * 0.85) / img.naturalHeight,
                1
            );
            imgEl.src = objectUrl;
            updateImageStyle();
            resetCrop();
        };
        img.src = objectUrl;
    }

    // ---------- Exportação da imagem final ----------

    // O servidor recusa requisições acima de ~5MB (erro 413). Um PNG de
    // 1920px de uma foto passa disso com facilidade, então fotos (sem
    // transparência) são exportadas em JPEG, que é bem menor. Só imagens com
    // transparência continuam em PNG (e vão para WebP se o PNG ficar grande).
    const MAX_PNG_BYTES = 3.5 * 1024 * 1024;

    function exportCanvas(canvas, done) {
        let hasAlpha = false;
        try {
            const px = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
            for (let i = 3; i < px.length; i += 4) {
                if (px[i] < 255) { hasAlpha = true; break; }
            }
        } catch (e) {
            hasAlpha = true;
        }

        if (!hasAlpha) {
            canvas.toBlob(done, 'image/jpeg', 0.92);
            return;
        }
        canvas.toBlob(function (png) {
            if (png && png.size > MAX_PNG_BYTES) {
                canvas.toBlob(function (webp) {
                    done(webp && webp.type === 'image/webp' ? webp : png);
                }, 'image/webp', 0.9);
            } else {
                done(png);
            }
        }, 'image/png');
    }

    // ---------- Ligação com os campos de arquivo da página ----------

    function attachPerspectiveEditor(input) {
        if (!input || input.dataset.peAttached) return;
        input.dataset.peAttached = '1';

        input.addEventListener('change', function () {
            const file = input.files && input.files[0];
            if (!file || file.type.indexOf('image/') !== 0) return;

            openEditor(file, function (blob) {
                const ext = blob.type === 'image/jpeg' ? 'jpg' : (blob.type === 'image/webp' ? 'webp' : 'png');
                const editedFile = new File([blob], 'imagem-ajustada.' + ext, { type: blob.type || 'image/png' });
                const dt = new DataTransfer();
                dt.items.add(editedFile);
                input.files = dt.files;
                // Evento próprio (não "change", pra não reabrir o editor):
                // avisa quem estiver de olho no input (ex: envio automático
                // do avatar) que o arquivo final (já recortado) está pronto.
                input.dispatchEvent(new Event('pe:change', { bubbles: true }));
            });
        });
    }

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('.perspective-upload').forEach(attachPerspectiveEditor);
    });

    window.attachPerspectiveEditor = attachPerspectiveEditor;
})();
