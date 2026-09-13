'use strict';

// The outer chat-body scrolls; chat-box is an expanding content column.
window.ConversationScroll = {
  create(box) {
    const owner = box.closest('.chat-body') || box;
    const topInset = 12;
    let anchor = null;
    let following = true;
    let expecting = false;
    let resizeFrame = 0;
    let alignFrame = 0;
    const anchorTop = () => owner.scrollTop + anchor.getBoundingClientRect().top
      - owner.getBoundingClientRect().top - topInset;
    const resizeSpace = () => {
      if (!anchor?.isConnected) return;
      const oldSpace = parseFloat(box.style.paddingBottom) || 0;
      const trailingHeight = box.getBoundingClientRect().bottom
        - anchor.getBoundingClientRect().bottom - oldSpace;
      // Leave enough room after the reply for even the final reply to reach the
      // reading origin. This also works when the conversation has min-height: 100%.
      const nextSpace = Math.max(0,
        owner.clientHeight - anchor.offsetHeight - trailingHeight - topInset);
      box.style.paddingBottom = Math.ceil(nextSpace) + 'px';
    };
    const stopFollowing = () => { following = false; };
    owner.addEventListener('wheel', stopFollowing, { passive: true });
    owner.addEventListener('touchmove', stopFollowing, { passive: true });
    owner.addEventListener('pointerdown', stopFollowing, { passive: true });
    owner.addEventListener('keydown', event => {
      if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) {
        stopFollowing();
      }
    });
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = requestAnimationFrame(resizeSpace);
    });
    observer.observe(owner);
    const reveal = (node, force = false) => {
      if (!node || (!following && !force)) return;
      if (anchor) observer.unobserve(anchor);
      anchor = node;
      observer.observe(anchor);
      resizeSpace();
      // Instant and only once per reply. Streaming growth never pulls readers to the bottom.
      cancelAnimationFrame(alignFrame);
      alignFrame = requestAnimationFrame(() => {
        if (anchor !== node || (!following && !force)) return;
        resizeSpace();
        owner.scrollTo({ top: Math.max(0, anchorTop()), behavior: 'auto' });
      });
    };
    return {
      owner,
      nearBottom: () => owner.scrollHeight - owner.scrollTop - owner.clientHeight < 48,
      begin: () => { following = true; expecting = true; },
      end: () => { expecting = false; },
      expecting: () => expecting,
      reveal,
      refresh: resizeSpace,
      reset: () => {
        cancelAnimationFrame(resizeFrame);
        cancelAnimationFrame(alignFrame);
        if (anchor) observer.unobserve(anchor);
        anchor = null;
        box.style.paddingBottom = '';
        following = true;
        expecting = false;
      },
    };
  },
};
