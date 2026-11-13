<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Qt/C++ client SDK

This static Qt 6 library provides Thoughts with authenticated requests and a crash-safe local operation outbox.

The library does not know about Okular document objects and can therefore be tested independently.

Okular integration will translate its canonical sidecar records to operation objects and apply downloaded changes to the existing in-memory annotation path.
