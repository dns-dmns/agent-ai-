JS_RECORDER_SCRIPT = """
(function() {
    if (window.hasPlaywrightRecorder) return;
    window.hasPlaywrightRecorder = true;

    function getUniqueSelector(el) {
        if (el.id) return `#${el.id}`;
        if (el.name) return `${el.tagName.toLowerCase()}[name="${el.name}"]`;
        if (el.placeholder) return `${el.tagName.toLowerCase()}[placeholder="${el.placeholder}"]`;
        
        if (el.tagName.toLowerCase() === 'button') {
            const text = el.innerText.trim();
            if (text) return `button:has-text("${text}")`;
        }
        
        let path = [];
        let current = el;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
            let selector = current.tagName.toLowerCase();
            if (current.id) {
                selector += '#' + current.id;
                path.unshift(selector);
                break;
            } else {
                let sib = current, nth = 1;
                while (sib = sib.previousElementSibling) {
                    if (sib.tagName === current.tagName) nth++;
                }
                if (nth > 1) selector += `:nth-of-type(${nth})`;
            }
            path.unshift(selector);
            current = current.parentNode;
        }
        return path.join(' > ');
    }

    document.addEventListener('click', (e) => {
        const target = e.target.closest('button, input, select, textarea, a, [role="button"]');
        if (!target) return;
        
        const selector = getUniqueSelector(target);
        const tag = target.tagName.toLowerCase();
        const text = target.innerText ? target.innerText.trim().substring(0, 50) : '';
        
        if (window.recordPlaywrightEvent) {
            window.recordPlaywrightEvent({
                action: 'CLICK',
                selector: selector,
                tag: tag,
                text: text,
                value: ''
            });
        }
    }, true);

    document.addEventListener('change', (e) => {
        const target = e.target;
        if (target.tagName.toLowerCase() !== 'input' && target.tagName.toLowerCase() !== 'textarea' && target.tagName.toLowerCase() !== 'select') return;
        
        const selector = getUniqueSelector(target);
        const tag = target.tagName.toLowerCase();
        
        if (window.recordPlaywrightEvent) {
            window.recordPlaywrightEvent({
                action: 'TYPE',
                selector: selector,
                tag: tag,
                text: '',
                value: target.value
            });
        }
    }, true);
})();
"""
