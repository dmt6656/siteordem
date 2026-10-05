/*
 * Sistema padrão de notificações e confirmações do site.
 *
 * Substitui os diálogos nativos do navegador (alert()/confirm() — os
 * populares "popups do Google Chrome") por um painel com a mesma
 * aparência do resto do site, igual em todas as páginas.
 *
 * Uso:
 *   showAlert('Mensagem de erro ou aviso');
 *   const ok = await askConfirm('Tem certeza?');
 *   <form onsubmit="return confirmDelete(this, 'Remover item?')">
 *   <button onclick="return confirmAndSubmit(event, 'Salvar mesmo assim?')">
 *   <form onsubmit="return confirmDeleteQty(this, 'Nome do item')">
 */
(function () {
    function injectMarkup() {
        if (document.getElementById('site-alert-overlay')) return;

        var wrap = document.createElement('div');
        wrap.innerHTML =
            '<div class="big-alert-overlay" id="site-alert-overlay">' +
                '<div class="big-alert-box">' +
                    '<button type="button" class="big-alert-close" id="site-alert-close-btn" aria-label="Fechar"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><title>x</title><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7L17 17M17 7L7 17"/></svg></button>' +
                    '<div class="big-alert-icon" id="site-alert-icon">⚠</div>' +
                    '<div class="big-alert-message" id="site-alert-message"></div>' +
                    '<div class="big-alert-total" id="site-alert-total"></div>' +
                    '<div class="big-alert-crit-badge" id="site-alert-crit-badge">🎯 CRÍTICO! 20 NATURAL!</div>' +
                '</div>' +
            '</div>' +
            '<div class="big-alert-overlay" id="site-confirm-overlay">' +
                '<div class="attack-panel-box">' +
                    '<button type="button" class="big-alert-close" id="site-confirm-cancel-btn" aria-label="Fechar"><svg xmlns="http://www.w3.org/2000/svg" width="1em" height="1em" viewBox="0 0 24 24"><title>x</title><path fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7L17 17M17 7L7 17"/></svg></button>' +
                    '<div class="attack-panel-title" id="site-confirm-message">Tem certeza?</div>' +
                    '<button type="button" class="btn-sm btn-delete" id="site-confirm-ok-btn">Confirmar</button>' +
                '</div>' +
            '</div>';

        while (wrap.firstChild) {
            document.body.appendChild(wrap.firstChild);
        }

        document.getElementById('site-alert-close-btn').addEventListener('click', closeAlert);
        document.getElementById('site-alert-overlay').addEventListener('click', function (e) {
            if (e.target.id === 'site-alert-overlay') closeAlert();
        });

        document.getElementById('site-confirm-ok-btn').addEventListener('click', function () {
            settleConfirm(true);
        });
        document.getElementById('site-confirm-cancel-btn').addEventListener('click', function () {
            settleConfirm(false);
        });
        document.getElementById('site-confirm-overlay').addEventListener('click', function (e) {
            if (e.target.id === 'site-confirm-overlay') settleConfirm(false);
        });
    }

    var alertTimer = null;
    function closeAlert() {
        var overlay = document.getElementById('site-alert-overlay');
        if (overlay) overlay.classList.remove('is-visible');
        clearTimeout(alertTimer);
    }

    // Som de rolagem de dados, tocado sempre que um resultado de rolagem é
    // exibido em qualquer página do site (popup de rolagem, alerta de
    // perícia ou mensagem de rolagem recebida no chat).
    //
    // O áudio é carregado já na abertura da página (a 1ª rolagem não atrasa nem
    // sai cortada) e o som não é reiniciado se acabou de tocar: o arquivo dura
    // ~0,55 s, e um 2º disparo logo em seguida fazia o som "gaguejar" ou soar 2x.
    var diceAudio = null;
    var lastDiceSoundAt = 0;
    var DICE_SOUND_MIN_GAP_MS = 900;
    try {
        diceAudio = new Audio('/static/sounds/rolagemdedados.mp3');
        diceAudio.preload = 'auto';
    } catch (e) {}
    window.playDiceSound = function () {
        try {
            var now = Date.now();
            if (now - lastDiceSoundAt < DICE_SOUND_MIN_GAP_MS) return;
            lastDiceSoundAt = now;
            if (!diceAudio) {
                diceAudio = new Audio('/static/sounds/rolagemdedados.mp3');
            }
            diceAudio.currentTime = 0;
            var p = diceAudio.play();
            if (p && p.catch) p.catch(function () {});
        } catch (e) {}
    };

    // Rolagens feitas por esta própria aba (ex.: o mestre rolando a perícia de um
    // jogador): o pop-up já tocou o som, então a mensagem que volta pelo chat
    // (que chega de outro "usuário") não pode tocar uma 2ª vez.
    var ownRollIds = {};
    window.markOwnRoll = function (id) {
        if (id !== undefined && id !== null) ownRollIds[id] = true;
    };
    window.isOwnRoll = function (id) {
        return !!ownRollIds[id];
    };

    // Substitui alert('mensagem').
    // opts.icon permite trocar o ícone padrão (⚠) por outro símbolo pontual.
    // opts.total, quando definido, exibe o número em destaque (grande e
    // colorido) abaixo da mensagem — usado para deixar o resultado final
    // de uma rolagem mais explícito em vez de ficar perdido no meio do texto.
    // opts.critical, quando true, destaca o pop-up (borda/selo) para
    // indicar que a rolagem tirou um 20 natural.
    window.showAlert = function (message, opts) {
        opts = opts || {};
        injectMarkup();
        var totalEl = document.getElementById('site-alert-total');
        if (opts.total !== undefined && opts.total !== null) {
            // Remove o "= <total>" final da mensagem para não repetir o
            // número — ele passa a aparecer só uma vez, em destaque.
            message = String(message).replace(/\s*=\s*[^=]*$/, '').trim();
            totalEl.textContent = '= ' + opts.total;
            totalEl.style.display = 'block';
        } else {
            totalEl.textContent = '';
            totalEl.style.display = 'none';
        }
        document.getElementById('site-alert-message').textContent = message;
        document.getElementById('site-alert-icon').textContent = opts.icon || '⚠';
        if (opts.icon === '🎲') {
            playDiceSound();
        }
        var box = document.querySelector('#site-alert-overlay .big-alert-box');
        var critBadge = document.getElementById('site-alert-crit-badge');
        var isCritical = !!opts.critical;
        if (box) box.classList.toggle('is-critical', isCritical);
        if (critBadge) critBadge.style.display = isCritical ? 'block' : 'none';
        document.getElementById('site-alert-overlay').classList.add('is-visible');
        clearTimeout(alertTimer);
        if (opts.autoClose !== false) {
            alertTimer = setTimeout(closeAlert, opts.duration || 4000);
        }
        return false;
    };

    // Mantido por compatibilidade com nome anterior específico de dinheiro.
    window.showMoneyAlert = function (message) {
        return window.showAlert(message, { duration: 3500 });
    };

    var confirmResolve = null;
    function settleConfirm(result) {
        var overlay = document.getElementById('site-confirm-overlay');
        if (overlay) overlay.classList.remove('is-visible');
        var resolve = confirmResolve;
        confirmResolve = null;
        if (resolve) resolve(result);
    }

    // Substitui confirm('mensagem'); retorna uma Promise<boolean>.
    window.askConfirm = function (message) {
        injectMarkup();
        document.getElementById('site-confirm-message').textContent = message;
        document.getElementById('site-confirm-overlay').classList.add('is-visible');
        return new Promise(function (resolve) {
            confirmResolve = resolve;
        });
    };

    // Uso em <form onsubmit="return confirmDelete(this, 'Mensagem?')">.
    // Bloqueia o envio nativo, pergunta e envia o formulário se confirmado.
    window.confirmDelete = function (form, message) {
        window.askConfirm(message).then(function (ok) {
            if (ok) form.submit();
        });
        return false;
    };

    // Uso em <button type="submit" onclick="return confirmAndSubmit(event, 'Mensagem?')">.
    window.confirmAndSubmit = function (event, message) {
        var btn = event.currentTarget || event.target;
        var form = btn.closest('form');
        window.askConfirm(message).then(function (ok) {
            if (ok && form) form.submit();
        });
        return false;
    };

    // Uso em formulários de exclusão de item que têm um <input name="quantity">
    // para o usuário escolher quantas unidades remover.
    window.confirmDeleteQty = function (form, itemName) {
        var qtyInput = form.querySelector('input[name="quantity"]');
        var max = qtyInput ? parseInt(qtyInput.max, 10) : 1;
        if (!max || max < 1) max = 1;
        var qty = qtyInput ? parseInt(qtyInput.value, 10) : 1;
        if (!qty || qty < 1) qty = 1;
        if (qty > max) qty = max;
        if (qtyInput) qtyInput.value = qty;

        var message = qty >= max
            ? ('Remover o item ' + itemName + (max > 1 ? (' (todas as ' + max + ' unidades)') : '') + '?')
            : ('Remover ' + qty + ' unidade' + (qty > 1 ? 's' : '') + ' de ' + itemName + '? (restam ' + (max - qty) + ')');

        return window.confirmDelete(form, message);
    };

    // Troca uma <img> que falhou ao carregar (o registro existe no banco,
    // mas o arquivo não foi encontrado no servidor) por um aviso de erro
    // explícito, em vez do ícone de imagem quebrada do navegador
    // sobrepondo o texto ao lado.
    // Uso: <img onerror="return imgFallback(this, 'item')">
    var IMG_FALLBACKS = {
        item: '<div class="item-img-placeholder img-load-error">404<br>imagem não carregada</div>',
        avatar: '<div class="avatar-placeholder img-load-error">404<br>imagem não carregada</div>',
        'avatar-sm': '<div class="avatar-placeholder avatar-placeholder-sm img-load-error" title="Erro 404: imagem não carregada">404</div>',
        'agent-avatar': '<div class="agent-avatar-placeholder img-load-error" title="Erro 404: imagem não carregada">404</div>',
        banner: '<div class="campaign-banner-placeholder img-load-error">404 - imagem não carregada</div>'
    };

    window.imgFallback = function (img, kind) {
        if (!img || img.dataset.fallbackApplied) return false;
        img.dataset.fallbackApplied = '1';
        var placeholderHtml = IMG_FALLBACKS[kind];
        if (!placeholderHtml) {
            img.style.display = 'none';
            return false;
        }
        var wrap = document.createElement('div');
        wrap.innerHTML = placeholderHtml;
        var placeholder = wrap.firstElementChild;
        if (placeholder && img.parentNode) {
            img.replaceWith(placeholder);
        } else {
            img.style.display = 'none';
        }
        return false;
    };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', injectMarkup);
    } else {
        injectMarkup();
    }

    // Indicador de mensagens não lidas na aba "Chat" da navegação,
    // visível em qualquer outra página do site. A própria página do
    // chat marca tudo como lido no servidor (ao carregar e a cada
    // poll), então aqui só precisamos consultar a contagem periodicamente.
    var UNREAD_POLL_MS = 8000;

    function pollChatUnread() {
        var link = document.getElementById('combat-chat-toggle');
        if (!link) return; // página sem o chat (ex: login)

        fetch('/chat/api/unread')
            .then(function (res) { return res.ok ? res.json() : null; })
            .then(function (data) {
                if (!data) return;
                // Chat aberto: o próprio chat já marca tudo como lido.
                var open = link.getAttribute('aria-expanded') === 'true';
                link.classList.toggle('has-unread', !!data.unread && !open);
                if (typeof data.sem_sanidade !== 'undefined' && document.body) {
                    document.body.classList.toggle('sem-sanidade', !!data.sem_sanidade);
                }
            })
            .catch(function () {
                // Falha silenciosa (ex: sessão expirada) — não interrompe a página.
            });
    }

    function initChatUnreadPolling() {
        if (!document.getElementById('combat-chat-toggle')) return;
        pollChatUnread();
        setInterval(pollChatUnread, UNREAD_POLL_MS);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initChatUnreadPolling);
    } else {
        initChatUnreadPolling();
    }

    // ---- Encolhe a folga de baixo do card quando o "Adicionar
    // Habilidade/Ritual/Item" está fechado (sem o formulário aberto),
    // pra ficar do mesmo tamanho da folga de cima do resumo. Feito via
    // classe + JS (em vez de só CSS com :has()) pra funcionar em
    // qualquer navegador, mesmo os que não suportam :has(). ----
    function syncFormDetailsSpacing(details) {
        var card = details.closest('.panel-section, .category-block, .user-card');
        if (!card) return;
        card.classList.toggle('form-collapsed', !details.open);
    }

    // ---- Seta (▸/▾) do "Adicionar Habilidade/Ritual/Item", controlada
    // por JS em vez de CSS ::before + [open]: assim ela nunca fica presa
    // apontando pra baixo, porque é atualizada no mesmo evento "toggle"
    // que já disparava o ajuste de espaçamento acima. ----
    function syncFormDetailsArrow(details) {
        var summary = details.querySelector('summary');
        if (!summary) return;
        var arrow = summary.querySelector('.add-form-details-arrow');
        if (!arrow) {
            arrow = document.createElement('span');
            arrow.className = 'add-form-details-arrow';
            summary.insertBefore(arrow, summary.firstChild);
        }
        arrow.textContent = details.open ? '\u25BE ' : '\u25B8 ';
    }

    function initFormDetailsSpacing() {
        document.querySelectorAll('.add-form-details').forEach(function (details) {
            syncFormDetailsSpacing(details);
            syncFormDetailsArrow(details);
            details.addEventListener('toggle', function () {
                syncFormDetailsSpacing(details);
                syncFormDetailsArrow(details);
            });
        });
        // "Ver / editar detalhes completos": mesma lógica de folga acima,
        // mas sem a seta via JS — esse "details" já tem sua própria seta
        // (▸/▾) desenhada em CSS puro (::before), então só sincronizamos
        // o espaçamento do card por baixo.
        document.querySelectorAll('.user-details').forEach(function (details) {
            syncFormDetailsSpacing(details);
            details.addEventListener('toggle', function () {
                syncFormDetailsSpacing(details);
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initFormDetailsSpacing);
    } else {
        initFormDetailsSpacing();
    }

    // ---- Ajuda os polls periódicos (ficha do jogador, agentes, campanha)
    // a pausar enquanto a pessoa está digitando em algum campo de texto,
    // evitando que uma atualização vinda do servidor sobrescreva o que
    // ela está escrevendo no meio da digitação. ----
    var TEXT_INPUT_TYPES = ['text', 'search', 'email', 'url', 'tel', 'password', 'number'];
    window.isTypingInField = function () {
        var el = document.activeElement;
        if (!el) return false;
        if (el.isContentEditable) return true;
        var tag = el.tagName;
        if (tag === 'TEXTAREA') return true;
        if (tag === 'INPUT') {
            var type = (el.type || 'text').toLowerCase();
            return TEXT_INPUT_TYPES.indexOf(type) !== -1;
        }
        return false;
    };
})();

/*
 * Auto-capitalização dos campos de texto ao cadastrar um ritual: deixa
 * maiúscula a primeira letra do campo e a primeira letra de cada frase
 * seguinte (após ., ! ou ?), conforme o jogador/mestre digita.
 */
(function () {
    function capitalizeSentences(text) {
        if (!text) return text;
        text = text.charAt(0).toUpperCase() + text.slice(1);
        return text.replace(/([.!?]\s+)([a-zà-ú])/g, function (match, sep, letter) {
            return sep + letter.toUpperCase();
        });
    }

    function attachAutoCapitalize(el) {
        el.addEventListener('input', function () {
            var start = el.selectionStart;
            var end = el.selectionEnd;
            var newValue = capitalizeSentences(el.value);
            if (newValue !== el.value) {
                el.value = newValue;
                if (start !== null) el.setSelectionRange(start, end);
            }
        });
    }

    function initRitualAutoCapitalize() {
        document.querySelectorAll('.add-ritual-form input[type="text"], .add-ritual-form textarea')
            .forEach(attachAutoCapitalize);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initRitualAutoCapitalize);
    } else {
        initRitualAutoCapitalize();
    }
})();

/*
 * Polling econômico: aba em segundo plano consulta o servidor 5x menos
 * e, ao voltar para a aba, atualiza na hora.
 */
window.pollWhenVisible = function (fn, ms) {
    var hiddenTicks = 0;
    setInterval(function () {
        if (document.hidden) {
            hiddenTicks++;
            if (hiddenTicks % 5 !== 0) return;
        } else {
            hiddenTicks = 0;
        }
        fn();
    }, ms);
    document.addEventListener('visibilitychange', function () {
        if (!document.hidden) fn();
    });
};

/* Nome do PE: no modo "Jogar sem Sanidade" ele aparece como PD.
   PE_T contém as duas versões; o CSS mostra só a que vale no momento
   (por isso a troca acontece ao vivo, sem recarregar). */
window.PE_T = '<span class="t-pe">PE</span><span class="t-pd">PD</span>';
window.peLabel = function () {
    return document.body && document.body.classList.contains('sem-sanidade') ? 'PD' : 'PE';
};


/* Clicar na foto de um item (inventário) amplia a imagem; clicar de novo
   ou apertar Esc fecha. As miniaturas pequenas do painel de combate não
   abrem, para não atrapalhar os cliques da lista. */
(function () {
    function closeLightbox() {
        var box = document.getElementById('item-lightbox');
        if (box) box.remove();
    }

    document.addEventListener('click', function (e) {
        var img = e.target.closest ? e.target.closest('img.item-img') : null;
        if (!img || img.classList.contains('combat-inv-thumb')) return;

        closeLightbox();
        var box = document.createElement('div');
        box.id = 'item-lightbox';
        box.className = 'item-lightbox';

        var big = document.createElement('img');
        big.src = img.src;
        big.alt = img.alt || '';
        box.appendChild(big);

        if (img.alt) {
            var cap = document.createElement('div');
            cap.className = 'item-lightbox-caption';
            cap.textContent = img.alt;
            box.appendChild(cap);
        }

        box.addEventListener('click', closeLightbox);
        document.body.appendChild(box);
    });

    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') closeLightbox();
    });
})();
