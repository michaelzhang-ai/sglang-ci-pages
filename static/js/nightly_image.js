(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    root.NightlyImage = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    const IMAGE_LABELS = Object.freeze({
        rocm10: '10.0.0',
        rocm724: '7.2.4',
        rocm720: '7.2.0',
        // Retired by sgl-project/sglang#38632, and still named by every job of
        // the runs the window reaches before it. Without the label the badge
        // reads `rocm700` and the row sorts as an unknown image.
        rocm700: '7.0.0',
    });
    // Newest first, matching the order in the upstream workflow matrix.
    const IMAGE_ORDER = Object.freeze(['rocm10', 'rocm724', 'rocm720', 'rocm700']);
    // Series whose jobs are one image each. The unified PR workflow is exposed
    // as two dashboard tracks; both still need the same legacy name parser.
    const PR_MATRIX_WFS = new Set(['pr_amd', 'pr_amd_rocm10', 'pr_amd_rocm72']);
    const IMAGE_MATRIX_WFS = new Set([...PR_MATRIX_WFS, 'amd', 'aiter_scout']);
    const PR_ROCM724_START = '2026-08-20';

    // The MI355X disagg nightly has no image matrix: each scheduled run
    // resolves the newest dated tag of one image line, which
    // sgl-project/sglang#41053 moved from `rocm720-mi35x` to `rocm10-mi35x`
    // before the 2026-09-24 run. Only the benchmark legs run on it; `setup`,
    // `collect-results` and a leg skipped before its matrix expanded do not.
    const DISAGG_WF = 'mi355x_disagg';
    const DISAGG_ROCM10_START = '2026-09-24';
    const DISAGG_LEG_RE = /^nightly-mi355x-\d+n \(/;

    // The Spur runners' scheduled copy of a PR-gate suite
    // (sgl-project/sglang#49053) runs rocm10 only, and its job names carry no
    // image.
    const SPUR_WF = 'spur_mi35x';

    // GitHub renders current matrix jobs as
    //   task (rocm10, linux-mi300-1gpu-sglang, 0)
    // and the first matrix rollout used
    //   task-rocm720 (rocm724)
    // Match both, then discard the runner label while retaining a partition.
    // The older form still reaches the page through the retired files'
    // history, which the two surviving series keep reading. AITER Scout runs
    // older than the consolidation named jobs this way too.
    const EXPLICIT_IMAGE_RE = /^(.*)\s+\((rocm\d+)(?:,\s*(.*))?\)\s*$/;
    const TRAILING_ARGS_RE = /^(.*)\s+\(([^()]*)\)\s*$/;
    const RUNNER_RE = /^linux-[a-z0-9._-]+$/i;

    function splitArgs(value) {
        return (value || '').split(',').map(part => part.trim()).filter(Boolean);
    }

    function canonicalJobName(prefix, args) {
        const name = prefix.trim().replace(/-rocm720$/, '');
        const details = splitArgs(args).filter(part => !RUNNER_RE.test(part));
        return details.length ? `${name} (${details.join(', ')})` : name;
    }

    function jobImage(wfKey, rawName, date = '') {
        const name = String(rawName || '');
        const explicit = EXPLICIT_IMAGE_RE.exec(name);
        if (explicit) {
            return {
                base: canonicalJobName(explicit[1], explicit[3]),
                image: explicit[2],
            };
        }

        if (wfKey === DISAGG_WF) {
            if (!DISAGG_LEG_RE.test(name)) return { base: name, image: '' };
            return { base: name, image: date >= DISAGG_ROCM10_START ? 'rocm10' : 'rocm720' };
        }

        if (wfKey === SPUR_WF) return { base: name, image: 'rocm10' };

        if (!IMAGE_MATRIX_WFS.has(wfKey)) return { base: name, image: '' };

        const trailing = TRAILING_ARGS_RE.exec(name);
        const prefix = trailing ? trailing[1] : name;
        const args = trailing ? trailing[2] : '';
        if (!/-rocm720$/.test(prefix)) return { base: name, image: '' };

        // Before image was included in the PR job name, test jobs still carried
        // a runner argument. Do not mistake shared gate jobs such as
        // wait-for-stage-a-amd-rocm720 for an image-specific result.
        const hasRunner = splitArgs(args).some(part => RUNNER_RE.test(part));
        if (PR_MATRIX_WFS.has(wfKey) && !hasRunner) return { base: name, image: '' };

        // The PR workflow moved its single scheduled image from 7.2.0 to 7.2.4
        // on this date. Nightly's suffix-less pre-matrix jobs were always 7.2.0.
        const image = PR_MATRIX_WFS.has(wfKey) && date >= PR_ROCM724_START
            ? 'rocm724'
            : 'rocm720';
        return { base: canonicalJobName(prefix, args), image };
    }

    function imageRank(image) {
        const index = IMAGE_ORDER.indexOf(image);
        return index === -1 ? IMAGE_ORDER.length : index;
    }

    return {
        IMAGE_LABELS,
        IMAGE_ORDER,
        IMAGE_MATRIX_WFS,
        jobImage,
        imageRank,
    };
}));
