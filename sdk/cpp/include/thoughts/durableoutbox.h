// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QList>
#include <QString>
#include <QStringList>

namespace Thoughts
{

struct AnnotationSubtypeRepairResult {
    bool success = false;
    qsizetype repaired = 0;
    QString backupPath;
    QByteArray backupSha256;
    QString error;
};

class DurableOutbox
{
public:
    explicit DurableOutbox(QString path);

    QJsonObject enqueue(QString entityType,
                        QString entityId,
                        QString collection,
                        qint64 baseRevision,
                        QJsonObject payload,
                        QString clientTimestamp);
    QJsonObject enqueueAgentProposalResolution(QString artifactSha256,
                                               QString action,
                                               QJsonArray proposals);
    QJsonObject enqueueAgentProposalUndo(QString operationId);
    QList<QJsonObject> operations(QString *error = nullptr) const;
    AnnotationSubtypeRepairResult repairBlankAnnotationSubtypes();
    bool acknowledge(const QList<QString> &operationIds, QString *error = nullptr);
    qint64 cursor(QString *error = nullptr) const;
    bool advanceCursor(qint64 expectedPrevious, qint64 next, QString *error = nullptr);

private:
    QJsonObject read(QString *error = nullptr) const;
    bool write(const QJsonObject &state, QString *error = nullptr) const;

    QString m_path;
};

}
