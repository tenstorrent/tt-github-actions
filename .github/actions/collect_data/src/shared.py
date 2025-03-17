# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

report_failure = False


def failure_happened():
    report_failure = True


def is_failure():
    return report_failure
