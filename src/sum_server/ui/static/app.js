/* OpenSUM Web UI behaviour. Hand-written, no framework, no build step.
 *
 * Two things live here:
 *   1. the host search bar (token chips + suggestion menu keyboard handling);
 *   2. the copy-to-clipboard buttons on the enrollment page;
 *   3. whole-row navigation on the host list;
 *   4. the change-history popovers.
 *
 * The search bar is only an editor over the existing query params
 * (q, presence, group, component, and repeatable fact / param). It serializes
 * tokens back to those params and lets HTMX fetch the rows, so the URL stays
 * shareable and the page renders identically without JavaScript.
 *
 * Token grammar (mirrors _search_to_tokens in ui/routes.py):
 *   presence:<state>  group:<name>  component:<text>
 *   fact:<key>=<value>  param:<key>=<value>
 *   anything else -> free text (the `q` param)
 */

(function () {
    "use strict";

    var FIELDS = ["presence", "group", "component", "fact", "param"];

    /* --- token model ---------------------------------------------------- */

    function parseToken(raw) {
        var idx = raw.indexOf(":");
        if (idx > 0) {
            var field = raw.slice(0, idx).toLowerCase();
            var rest = raw.slice(idx + 1);
            if (FIELDS.indexOf(field) !== -1 && rest !== "") {
                return { field: field, value: rest, raw: raw };
            }
        }
        return { field: "q", value: raw, raw: raw };
    }

    /* Tokens -> URLSearchParams. fact/param carry key=value, which the server
       reads as the key:value form its query params already use. */
    function tokensToParams(tokens) {
        var params = new URLSearchParams();
        var text = [];
        tokens.forEach(function (t) {
            if (t.field === "q") {
                text.push(t.value);
            } else if (t.field === "fact" || t.field === "param") {
                var eq = t.value.indexOf("=");
                if (eq > 0) {
                    params.append(t.field, t.value.slice(0, eq) + ":" + t.value.slice(eq + 1));
                }
            } else {
                params.set(t.field, t.value);
            }
        });
        if (text.length) {
            params.set("q", text.join(" "));
        }
        return params;
    }

    function chipLabel(t) {
        if (t.field === "q") return 'text: "' + t.value + '"';
        if (t.field === "presence") return "state: " + t.value;
        if (t.field === "fact" || t.field === "param") {
            return t.field + " " + t.value.replace("=", " = ");
        }
        return t.field + ": " + t.value;
    }

    /* --- search bar ------------------------------------------------------ */

    function initSearchBar(bar) {
        var input = bar.querySelector("[data-search-input]");
        var chipRow = bar.querySelector("[data-chip-row]");
        var menu = bar.querySelector("[data-suggest]");
        var results = document.querySelector("[data-search-results]");
        if (!input || !chipRow || !menu || !results) return;

        var tokens = (bar.dataset.tokens || "")
            .split(" ")
            .filter(Boolean)
            .map(parseToken);
        var selected = -1;

        function renderChips() {
            chipRow.innerHTML = "";
            tokens.forEach(function (t, i) {
                var chip = document.createElement("span");
                chip.className = "chip";
                chip.appendChild(document.createTextNode(chipLabel(t)));
                var x = document.createElement("button");
                x.type = "button";
                x.className = "chip-x";
                x.setAttribute("aria-label", "Remove filter " + chipLabel(t));
                x.textContent = "×";
                x.addEventListener("click", function () {
                    tokens.splice(i, 1);
                    renderChips();
                    submit();
                });
                chip.appendChild(x);
                chipRow.appendChild(chip);
            });
        }

        /* Refetch the rows for the current tokens and mirror the same params
           into the address bar, so the URL stays shareable. replaceState, not
           pushState: live typing should not bury the back button. */
        function submit() {
            var qs = tokensToParams(tokens).toString();
            var suffix = qs ? "?" + qs : "";
            window.htmx.ajax("GET", "/hosts/rows" + suffix, {
                target: results,
                swap: "innerHTML",
            });
            history.replaceState(null, "", "/hosts" + suffix);
        }

        function closeMenu() {
            menu.hidden = true;
            selected = -1;
        }

        function items() {
            return Array.prototype.slice.call(menu.querySelectorAll(".suggest-item"));
        }

        function highlight(next) {
            var list = items();
            if (!list.length) return;
            if (selected >= 0 && list[selected]) {
                list[selected].removeAttribute("aria-selected");
            }
            selected = (next + list.length) % list.length;
            list[selected].setAttribute("aria-selected", "true");
            list[selected].scrollIntoView({ block: "nearest" });
        }

        function commit(value) {
            /* A field prefix ("presence:") stays in the input so the next
               request suggests its values; a complete token becomes a chip. */
            if (value.charAt(value.length - 1) === ":" || value.charAt(value.length - 1) === "=") {
                input.value = value;
                requestSuggestions();
                return;
            }
            tokens.push(parseToken(value));
            input.value = "";
            renderChips();
            closeMenu();
            submit();
        }

        function requestSuggestions() {
            window.htmx.ajax("GET", "/hosts/suggest?token=" + encodeURIComponent(input.value), {
                target: menu,
                swap: "innerHTML",
            });
        }

        input.addEventListener("input", function () {
            menu.hidden = false;
            requestSuggestions();
        });

        input.addEventListener("focus", function () {
            menu.hidden = false;
            requestSuggestions();
        });

        input.addEventListener("keydown", function (e) {
            if (e.key === "ArrowDown") {
                e.preventDefault();
                menu.hidden = false;
                highlight(selected + 1);
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                highlight(selected - 1);
            } else if (e.key === "Enter") {
                e.preventDefault();
                var list = items();
                if (!menu.hidden && selected >= 0 && list[selected]) {
                    commit(list[selected].dataset.value);
                } else if (input.value.trim()) {
                    commit(input.value.trim());
                }
            } else if (e.key === "Escape") {
                closeMenu();
            } else if (e.key === "Backspace" && input.value === "" && tokens.length) {
                tokens.pop();
                renderChips();
                submit();
            }
        });

        menu.addEventListener("click", function (e) {
            var item = e.target.closest(".suggest-item");
            if (item) commit(item.dataset.value);
        });

        /* Re-highlight nothing when a fresh suggestion list lands. */
        menu.addEventListener("htmx:afterSwap", function () {
            selected = -1;
        });

        document.addEventListener("click", function (e) {
            if (!bar.contains(e.target)) closeMenu();
        });

        renderChips();
    }

    /* --- copy buttons (enrollment wizard) -------------------------------- */

    function initCopyButtons() {
        document.querySelectorAll(".copy-btn").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var block = document.getElementById(btn.dataset.target);
                var pre = block && block.querySelector("pre");
                if (!pre) return;
                navigator.clipboard.writeText(pre.textContent).then(function () {
                    btn.textContent = "Copied";
                    setTimeout(function () {
                        btn.textContent = "Copy";
                    }, 1500);
                });
            });
        });
    }

    /* --- history popovers ------------------------------------------------ */

    /* HTMX fetches the timeline into the adjacent .hist-pop; this only opens,
       closes, keeps one open at a time, and places it. Delegated, so controls
       swapped in by HTMX work without re-binding. */
    function closeHistory(except) {
        document.querySelectorAll(".hist-pop").forEach(function (pop) {
            if (pop === except) return;
            pop.hidden = true;
            var btn = pop.parentElement && pop.parentElement.querySelector("[data-hist-toggle]");
            if (btn) btn.setAttribute("aria-expanded", "false");
        });
    }

    /* The popover is `position: fixed` so a table's scroll container cannot
       clip it, which means its coordinates are ours to supply. Right-align to
       the button, flip above when there is no room below, and keep it inside
       the viewport on both axes. */
    function placeHistory(btn, pop) {
        pop.style.top = "0px";
        pop.style.left = "0px";
        var b = btn.getBoundingClientRect();
        var p = pop.getBoundingClientRect();
        var margin = 8;
        var below = window.innerHeight - b.bottom;
        var top = below >= p.height + margin ? b.bottom + 6 : b.top - p.height - 6;
        var left = b.right - p.width;
        pop.style.top = Math.max(margin, Math.min(top, window.innerHeight - p.height - margin)) + "px";
        pop.style.left = Math.max(margin, Math.min(left, window.innerWidth - p.width - margin)) + "px";
    }

    /* Whole-row navigation on result tables.

       The row's own anchor stays the keyboard and no-JavaScript path; this
       only widens the click target to the rest of the row. Delegated from the
       document because HTMX swaps these rows out from under any listener
       bound to them. */
    function rowLinkTarget(ev) {
        var el = ev.target;
        if (!el || !el.closest) return null;
        var row = el.closest("tr[data-row-href]");
        if (!row) return null;
        /* A click on something that already does its own thing is that
           thing's click, not the row's. */
        if (el.closest("a, button, input, select, textarea, label")) return null;
        /* Finishing a text selection inside the row is not a click on it. */
        var sel = window.getSelection();
        if (sel && !sel.isCollapsed && row.contains(sel.anchorNode)) return null;
        return row.dataset.rowHref;
    }

    function initRowLinks() {
        document.addEventListener("click", function (ev) {
            var href = rowLinkTarget(ev);
            if (!href) return;
            if (ev.metaKey || ev.ctrlKey) {
                window.open(href, "_blank", "noopener");
            } else if (!ev.shiftKey) {
                window.location.assign(href);
            }
        });

        /* Middle-click opens a background tab, the way it would on a link. */
        document.addEventListener("auxclick", function (ev) {
            if (ev.button !== 1) return;
            var href = rowLinkTarget(ev);
            if (!href) return;
            ev.preventDefault();
            window.open(href, "_blank", "noopener");
        });
    }

    function initHistory() {
        document.addEventListener("click", function (ev) {
            var btn = ev.target.closest("[data-hist-toggle]");
            if (!btn) {
                if (!ev.target.closest(".hist-pop")) closeHistory(null);
                return;
            }
            var pop = btn.parentElement.querySelector(".hist-pop");
            if (!pop) return;
            var opening = pop.hidden;
            closeHistory(pop);
            pop.hidden = !opening;
            btn.setAttribute("aria-expanded", opening ? "true" : "false");
            if (opening) placeHistory(btn, pop);
        });

        /* The body arrives after the click, so the size we measured was the
           empty box; place it again once HTMX has swapped the timeline in. */
        document.body.addEventListener("htmx:afterSwap", function (ev) {
            var pop = ev.target;
            if (!pop.classList || !pop.classList.contains("hist-pop") || pop.hidden) return;
            var btn = pop.parentElement.querySelector("[data-hist-toggle]");
            if (btn) placeHistory(btn, pop);
        });

        document.addEventListener("keydown", function (ev) {
            if (ev.key === "Escape") closeHistory(null);
        });

        /* Fixed coordinates go stale the moment anything scrolls. Capture, so
           a table scrolling under the popover counts too. */
        document.addEventListener("scroll", function () { closeHistory(null); }, true);
        window.addEventListener("resize", function () { closeHistory(null); });
    }

    document.addEventListener("DOMContentLoaded", function () {
        var bar = document.querySelector("[data-searchbar]");
        if (bar) initSearchBar(bar);
        initCopyButtons();
        initRowLinks();
        initHistory();
    });
})();
