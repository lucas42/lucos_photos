/**
 * Lightbox module for lucos_photos.
 *
 * Usage:
 *   1. Include the lightbox HTML in your page (see lightbox markup below).
 *   2. Call initLightbox() after the DOM is ready.
 *   3. Add data-original-url and data-media-type attributes to each
 *      .media-card-link element — the lightbox auto-binds to them.
 *
 * Lightbox HTML (add inside your template):
 *   <div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="Media viewer">
 *       <button class="lightbox-close" id="lightbox-close" aria-label="Close">&times;</button>
 *       <button class="lightbox-nav lightbox-prev" id="lightbox-prev" aria-label="Previous">&lsaquo;</button>
 *       <button class="lightbox-nav lightbox-next" id="lightbox-next" aria-label="Next">&rsaquo;</button>
 *       <div class="lightbox-content">
 *           <div class="lightbox-inner" id="lightbox-inner"></div>
 *           <div class="lightbox-metadata" id="lightbox-metadata"></div>
 *       </div>
 *   </div>
 */
(function () {
    'use strict';

    let lightbox, lightboxInner, lightboxClose, lightboxPrev, lightboxNext, lightboxMetadata;
    let mediaItems = [];
    let currentIndex = -1;
    let triggerElement = null;
    const metadataCache = {};
    let currentFetchId = 0;
    let initialized = false;

    function formatDate(isoString) {
        if (!isoString) return null;
        try {
            const d = new Date(isoString);
            return d.toLocaleDateString(undefined, {
                year: 'numeric', month: 'long', day: 'numeric',
                hour: '2-digit', minute: '2-digit'
            });
        } catch (e) {
            return isoString;
        }
    }

    function addMetadataRow(dl, label, valueText) {
        const dt = document.createElement('dt');
        dt.textContent = label;
        const dd = document.createElement('dd');
        dd.textContent = valueText;
        dl.appendChild(dt);
        dl.appendChild(dd);
    }

    function renderMetadata(data, container) {
        container.innerHTML = '';

        const dl = document.createElement('dl');
        dl.className = 'lightbox-metadata-list';

        if (data.takenAt) {
            addMetadataRow(dl, 'Taken', formatDate(data.takenAt));
        }

        if (data.width && data.height) {
            const dt = document.createElement('dt');
            dt.textContent = 'Dimensions';
            const dd = document.createElement('dd');
            dd.textContent = data.width + ' \u00D7 ' + data.height;
            dl.appendChild(dt);
            dl.appendChild(dd);
        }

        if (data.fileExtension) {
            addMetadataRow(dl, 'Format', data.fileExtension.toUpperCase());
        }

        if (data.people && data.people.length > 0) {
            const dt = document.createElement('dt');
            dt.textContent = 'People';
            const dd = document.createElement('dd');
            dd.className = 'lightbox-people';

            data.people.forEach(function (person) {
                const link = document.createElement('a');
                link.href = '/people/' + encodeURIComponent(person.id);
                link.className = 'lightbox-person-link' + (person.isBackground ? ' lightbox-person-background' : '');

                if (person.profilePictureUrl) {
                    const pic = document.createElement('img');
                    pic.className = 'lightbox-person-pic';
                    pic.src = person.profilePictureUrl;
                    pic.alt = '';
                    link.appendChild(pic);
                }

                const nameSpan = document.createElement('span');
                nameSpan.textContent = person.name || 'Unknown';
                link.appendChild(nameSpan);
                dd.appendChild(link);
            });

            dl.appendChild(dt);
            dl.appendChild(dd);
        }

        container.appendChild(dl);

        const detailsLink = document.createElement('a');
        detailsLink.href = '/photos/' + encodeURIComponent(data.id);
        detailsLink.className = 'lightbox-details-link';
        detailsLink.textContent = 'View full details';
        container.appendChild(detailsLink);
    }

    function fetchAndShowMetadata(photoId) {
        if (!lightboxMetadata || !photoId) {
            if (lightboxMetadata) lightboxMetadata.innerHTML = '';
            return;
        }

        if (metadataCache[photoId]) {
            renderMetadata(metadataCache[photoId], lightboxMetadata);
            return;
        }

        const loadingMsg = document.createElement('p');
        loadingMsg.className = 'lightbox-metadata-loading';
        loadingMsg.textContent = 'Loading details\u2026';
        lightboxMetadata.innerHTML = '';
        lightboxMetadata.appendChild(loadingMsg);

        const fetchId = ++currentFetchId;
        fetch('/photos/' + encodeURIComponent(photoId), { headers: { 'Accept': 'application/json' } })
            .then(function (res) {
                if (!res.ok) throw new Error('Failed to load');
                return res.json();
            })
            .then(function (data) {
                metadataCache[photoId] = data;
                if (fetchId === currentFetchId) {
                    renderMetadata(data, lightboxMetadata);
                }
            })
            .catch(function () {
                if (fetchId === currentFetchId) {
                    lightboxMetadata.innerHTML = '';
                }
            });
    }

    function showMedia(index) {
        if (index < 0 || index >= mediaItems.length) return;
        currentIndex = index;

        lightboxInner.innerHTML = '';

        const item = mediaItems[index];
        if (item.mediaType === 'video') {
            const video = document.createElement('video');
            video.src = item.originalUrl;
            video.controls = true;
            video.autoplay = true;
            video.preload = 'metadata';
            lightboxInner.appendChild(video);
        } else {
            const img = document.createElement('img');
            img.src = item.originalUrl;
            img.alt = 'Photo';
            lightboxInner.appendChild(img);
        }

        // Update nav button visibility
        lightboxPrev.style.display = index > 0 ? '' : 'none';
        lightboxNext.style.display = index < mediaItems.length - 1 ? '' : 'none';

        // Fetch and display metadata
        fetchAndShowMetadata(item.id);
    }

    function openLightbox(index) {
        triggerElement = document.activeElement;
        showMedia(index);
        lightbox.classList.add('open');
        document.body.style.overflow = 'hidden';
        lightboxClose.focus();
    }

    function closeLightbox() {
        lightbox.classList.remove('open');
        document.body.style.overflow = '';
        const video = lightboxInner.querySelector('video');
        if (video) video.pause();
        lightboxInner.innerHTML = '';
        if (lightboxMetadata) lightboxMetadata.innerHTML = '';
        currentIndex = -1;
        if (triggerElement) {
            triggerElement.focus();
            triggerElement = null;
        }
    }

    function prevMedia() {
        if (currentIndex > 0) showMedia(currentIndex - 1);
    }

    function nextMedia() {
        if (currentIndex < mediaItems.length - 1) showMedia(currentIndex + 1);
    }

    /**
     * Initialise the lightbox.
     *
     * @param {Array} [items] - optional array of {originalUrl, mediaType, id}.
     *   If omitted, items are collected from .media-card-link[data-original-url]
     *   elements in the DOM.
     */
    function initLightbox(items) {
        lightbox = document.getElementById('lightbox');
        lightboxInner = document.getElementById('lightbox-inner');
        lightboxClose = document.getElementById('lightbox-close');
        lightboxPrev = document.getElementById('lightbox-prev');
        lightboxNext = document.getElementById('lightbox-next');
        lightboxMetadata = document.getElementById('lightbox-metadata');

        if (!lightbox) return;

        // Build the media list
        if (items) {
            mediaItems = items;
        } else {
            mediaItems = [];
            document.querySelectorAll('.media-card-link[data-original-url]').forEach(function (link) {
                mediaItems.push({
                    originalUrl: link.getAttribute('data-original-url'),
                    mediaType: link.getAttribute('data-media-type') || 'image',
                    id: link.getAttribute('data-photo-id') || '',
                });
            });
        }

        // Only bind event listeners once — safe to call initLightbox() multiple times
        // to update the items array (e.g. after dynamic content updates).
        if (initialized) return;
        initialized = true;

        // Bind click handlers on .media-card-link elements
        document.querySelectorAll('.media-card-link[data-original-url]').forEach(function (link, idx) {
            link.addEventListener('click', function (e) {
                if (e.button === 0 && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
                    e.preventDefault();
                    openLightbox(idx);
                }
            });
        });

        // Close handlers
        lightboxClose.addEventListener('click', closeLightbox);
        lightbox.addEventListener('click', function (e) {
            if (e.target === lightbox) closeLightbox();
        });

        // Nav handlers
        lightboxPrev.addEventListener('click', function (e) {
            e.stopPropagation();
            prevMedia();
        });
        lightboxNext.addEventListener('click', function (e) {
            e.stopPropagation();
            nextMedia();
        });

        // Keyboard navigation and focus trapping
        document.addEventListener('keydown', function (e) {
            if (!lightbox.classList.contains('open')) return;
            if (e.key === 'Escape') closeLightbox();
            if (e.key === 'ArrowLeft') prevMedia();
            if (e.key === 'ArrowRight') nextMedia();

            // Focus trapping — keep Tab within the lightbox
            if (e.key === 'Tab') {
                const focusable = lightbox.querySelectorAll('button:not([style*="display: none"]), a[href]');
                if (focusable.length === 0) return;
                const first = focusable[0];
                const last = focusable[focusable.length - 1];
                if (e.shiftKey) {
                    if (document.activeElement === first) {
                        e.preventDefault();
                        last.focus();
                    }
                } else {
                    if (document.activeElement === last) {
                        e.preventDefault();
                        first.focus();
                    }
                }
            }
        });
    }

    // Export for both module and global usage
    window.initLightbox = initLightbox;
    window.openLightbox = openLightbox;
    window.closeLightbox = closeLightbox;
})();
