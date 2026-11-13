// SPDX-License-Identifier: GPL-2.0-or-later
#include "thoughts/durableoutbox.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QLockFile>
#include <QSaveFile>
#include <QUuid>
#include <utility>

namespace Thoughts
{

namespace
{
bool lockOutbox(QLockFile &lock, QString *error)
{
    lock.setStaleLockTime(30000);
    if (lock.tryLock(5000)) {
        return true;
    }
    if (error) {
        *error = QStringLiteral("Could not lock the durable outbox: %1").arg(static_cast<int>(lock.error()));
    }
    return false;
}
}

DurableOutbox::DurableOutbox(QString path)
    : m_path(std::move(path))
{
}

QJsonObject DurableOutbox::read(QString *error) const
{
    QFile file(m_path);
    if (!file.exists()) {
        return QJsonObject{{QStringLiteral("next_device_sequence"), 1},
                           {QStringLiteral("server_cursor"), 0},
                           {QStringLiteral("operations"), QJsonArray()}};
    }
    if (!file.open(QIODevice::ReadOnly)) {
        if (error) {
            *error = file.errorString();
        }
        return {};
    }
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        if (error) {
            *error = parseError.errorString();
        }
        return {};
    }
    return document.object();
}

bool DurableOutbox::write(const QJsonObject &state, QString *error) const
{
    QDir().mkpath(QFileInfo(m_path).absolutePath());
    QSaveFile file(m_path);
    file.setDirectWriteFallback(false);
    if (!file.open(QIODevice::WriteOnly)) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    if (file.write(QJsonDocument(state).toJson(QJsonDocument::Compact) + '\n') < 0 || !file.commit()) {
        if (error) {
            *error = file.errorString();
        }
        return false;
    }
    return true;
}

QJsonObject DurableOutbox::enqueue(QString entityType,
                                   QString entityId,
                                   QString collection,
                                   qint64 baseRevision,
                                   QJsonObject payload,
                                   QString clientTimestamp)
{
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, nullptr)) {
        return {};
    }
    QJsonObject state = read();
    const qint64 sequence = state.value(QStringLiteral("next_device_sequence")).toInteger(1);
    QJsonObject operation{{QStringLiteral("operation_id"), QUuid::createUuid().toString(QUuid::WithoutBraces)},
                          {QStringLiteral("device_sequence"), sequence},
                          {QStringLiteral("entity_type"), std::move(entityType)},
                          {QStringLiteral("entity_id"), std::move(entityId)},
                          {QStringLiteral("collection"), std::move(collection)},
                          {QStringLiteral("client_timestamp"), std::move(clientTimestamp)},
                          {QStringLiteral("payload"), std::move(payload)}};
    if (baseRevision >= 0) {
        operation.insert(QStringLiteral("base_revision"), baseRevision);
    }
    QJsonArray operations = state.value(QStringLiteral("operations")).toArray();
    operations.append(operation);
    state.insert(QStringLiteral("operations"), operations);
    state.insert(QStringLiteral("next_device_sequence"), sequence + 1);
    return write(state) ? operation : QJsonObject();
}

QJsonObject DurableOutbox::enqueueAgentProposalResolution(QString artifactSha256,
                                                           QString action,
                                                           QJsonArray proposals)
{
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, nullptr)) {
        return {};
    }
    QJsonObject state = read();
    const qint64 sequence = state.value(QStringLiteral("next_device_sequence")).toInteger(1);
    QJsonObject operation{{QStringLiteral("outbox_kind"), QStringLiteral("agent_proposal_resolution")},
                          {QStringLiteral("operation_id"), QUuid::createUuid().toString(QUuid::WithoutBraces)},
                          {QStringLiteral("device_sequence"), sequence},
                          {QStringLiteral("artifact_sha256"), std::move(artifactSha256)},
                          {QStringLiteral("action"), std::move(action)},
                          {QStringLiteral("proposals"), std::move(proposals)}};
    QJsonArray operations = state.value(QStringLiteral("operations")).toArray();
    operations.append(operation);
    state.insert(QStringLiteral("operations"), operations);
    state.insert(QStringLiteral("next_device_sequence"), sequence + 1);
    return write(state) ? operation : QJsonObject();
}

QJsonObject DurableOutbox::enqueueAgentProposalUndo(QString operationId)
{
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, nullptr)) {
        return {};
    }
    QJsonObject state = read();
    const qint64 sequence = state.value(QStringLiteral("next_device_sequence")).toInteger(1);
    QJsonObject operation{{QStringLiteral("outbox_kind"), QStringLiteral("agent_proposal_undo")},
                          {QStringLiteral("operation_id"), QUuid::createUuid().toString(QUuid::WithoutBraces)},
                          {QStringLiteral("device_sequence"), sequence},
                          {QStringLiteral("undo_of"), std::move(operationId)}};
    QJsonArray operations = state.value(QStringLiteral("operations")).toArray();
    operations.append(operation);
    state.insert(QStringLiteral("operations"), operations);
    state.insert(QStringLiteral("next_device_sequence"), sequence + 1);
    return write(state) ? operation : QJsonObject();
}

QList<QJsonObject> DurableOutbox::operations(QString *error) const
{
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, error)) {
        return {};
    }
    QList<QJsonObject> output;
    const QJsonObject state = read(error);
    for (const QJsonValue value : state.value(QStringLiteral("operations")).toArray()) {
        if (value.isObject()) {
            output.append(value.toObject());
        }
    }
    return output;
}

AnnotationSubtypeRepairResult DurableOutbox::repairBlankAnnotationSubtypes()
{
    AnnotationSubtypeRepairResult result;
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, &result.error)) {
        return result;
    }

    QFile source(m_path);
    if (!source.exists()) {
        result.success = true;
        return result;
    }
    if (!source.open(QIODevice::ReadOnly)) {
        result.error = source.errorString();
        return result;
    }
    const QByteArray original = source.readAll();
    source.close();

    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(original, &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        result.error = parseError.error != QJsonParseError::NoError
            ? parseError.errorString()
            : QStringLiteral("The durable outbox root is not a JSON object.");
        return result;
    }

    QJsonObject state = document.object();
    QJsonArray operations = state.value(QStringLiteral("operations")).toArray();
    for (qsizetype index = 0; index < operations.size(); ++index) {
        QJsonObject operation = operations.at(index).toObject();
        if (operation.value(QStringLiteral("entity_type")).toString() != QLatin1String("annotation")) {
            continue;
        }
        QJsonObject payload = operation.value(QStringLiteral("payload")).toObject();
        const QJsonValue subtype = payload.value(QStringLiteral("subtype"));
        if (!subtype.isString() || !subtype.toString().trimmed().isEmpty()) {
            continue;
        }
        payload.insert(QStringLiteral("subtype"), QStringLiteral("Text"));
        operation.insert(QStringLiteral("payload"), payload);
        operations.replace(index, operation);
        ++result.repaired;
    }
    if (result.repaired == 0) {
        result.success = true;
        return result;
    }

    result.backupPath = m_path + QStringLiteral(".backup-")
        + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd'T'HHmmsszzz'Z'"));
    for (int suffix = 1; QFileInfo::exists(result.backupPath); ++suffix) {
        result.backupPath = m_path + QStringLiteral(".backup-")
            + QDateTime::currentDateTimeUtc().toString(QStringLiteral("yyyyMMdd'T'HHmmsszzz'Z'"))
            + QStringLiteral("-%1").arg(suffix);
    }
    if (!QFile::copy(m_path, result.backupPath)) {
        result.error = QStringLiteral("Could not create the durable outbox backup at %1.").arg(result.backupPath);
        result.backupPath.clear();
        return result;
    }
    result.backupSha256 = QCryptographicHash::hash(original, QCryptographicHash::Sha256).toHex();

    state.insert(QStringLiteral("operations"), operations);
    if (!write(state, &result.error)) {
        return result;
    }
    result.success = true;
    return result;
}

bool DurableOutbox::acknowledge(const QList<QString> &operationIds, QString *error)
{
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, error)) {
        return false;
    }
    QJsonObject state = read(error);
    QJsonArray retained;
    for (const QJsonValue value : state.value(QStringLiteral("operations")).toArray()) {
        const QJsonObject operation = value.toObject();
        if (!operationIds.contains(operation.value(QStringLiteral("operation_id")).toString())) {
            retained.append(operation);
        }
    }
    state.insert(QStringLiteral("operations"), retained);
    return write(state, error);
}

qint64 DurableOutbox::cursor(QString *error) const
{
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, error)) {
        return 0;
    }
    return read(error).value(QStringLiteral("server_cursor")).toInteger();
}

bool DurableOutbox::advanceCursor(qint64 expectedPrevious, qint64 next, QString *error)
{
    QLockFile lock(m_path + QStringLiteral(".lock"));
    if (!lockOutbox(lock, error)) {
        return false;
    }
    QJsonObject state = read(error);
    if (state.value(QStringLiteral("server_cursor")).toInteger() != expectedPrevious || next < expectedPrevious) {
        if (error) {
            *error = QStringLiteral("server cursor changed concurrently or moved backwards");
        }
        return false;
    }
    state.insert(QStringLiteral("server_cursor"), next);
    return write(state, error);
}

}
