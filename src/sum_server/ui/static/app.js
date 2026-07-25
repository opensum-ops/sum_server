/* OpenSUM Web UI behaviour. Hand-written, no framework, no build step.
 *
 * Two things live here:
 *   1. the host search bar (token chips + suggestion menu keyboard handling);
 *   2. the copy-to-clipboard buttons on the enrollment page.
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

    document.addEventListener("DOMContentLoaded", function () {
        var bar = document.querySelector("[data-searchbar]");
        if (bar) initSearchBar(bar);
        initCopyButtons();
    });
})();
