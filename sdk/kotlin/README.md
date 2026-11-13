<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Kotlin client SDK

This pure Kotlin/JVM module contains the repository protocol models, durable outbox, cursor state, and blocking transport used by Thoughts for Android.

The implementation uses `HttpURLConnection` so Android does not need an additional HTTP stack.

Network calls must run from an Android background worker rather than the UI thread.

The application remains responsible for mapping server changes into its Room database and for scheduling retries with WorkManager.
