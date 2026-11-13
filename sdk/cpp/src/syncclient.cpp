// SPDX-License-Identifier: GPL-2.0-or-later
#include "thoughts/syncclient.h"

#include <QCryptographicHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QNetworkRequest>
#include <QSet>
#include <QStringList>
#include <QTimer>
#include <QUrlQuery>
#include <utility>
#ifdef Q_OS_MACOS
#include <CommonCrypto/CommonDigest.h>
#endif

namespace Thoughts
{

static QPair<qsizetype, qsizetype> traceEventBoundary(const QByteArray &bytes)
{
    const auto lineEndingLength = [&bytes](qsizetype position) -> qsizetype {
        if (position < 0 || position >= bytes.size()) {
            return 0;
        }
        if (bytes.at(position) == '\n') {
            return 1;
        }
        if (bytes.at(position) != '\r') {
            return 0;
        }
        return position + 1 < bytes.size() && bytes.at(position + 1) == '\n' ? 2 : 1;
    };
    for (qsizetype position = 0; position < bytes.size(); ++position) {
        const qsizetype first = lineEndingLength(position);
        if (first == 0) {
            continue;
        }
        const qsizetype second = lineEndingLength(position + first);
        if (second > 0) {
            return {position, first + second};
        }
        position += first - 1;
    }
    return {-1, 0};
}

QList<TraceSseFrame> TraceSseParser::feed(const QByteArray &chunk)
{
    m_pending.append(chunk);
    return drain(false);
}

QList<TraceSseFrame> TraceSseParser::finish()
{
    return drain(true);
}

QList<TraceSseFrame> TraceSseParser::drain(bool complete)
{
    QList<TraceSseFrame> frames;
    while (true) {
        const auto [boundary, boundaryLength] = traceEventBoundary(m_pending);
        if (boundary < 0) {
            if (complete && !m_pending.isEmpty()) {
                frames.append(parseBlock(std::exchange(m_pending, {})));
            }
            return frames;
        }
        const QByteArray block = m_pending.left(boundary);
        m_pending.remove(0, boundary + boundaryLength);
        if (!block.isEmpty()) {
            frames.append(parseBlock(block));
        }
    }
}

TraceSseFrame TraceSseParser::parseBlock(const QByteArray &block)
{
    TraceSseFrame frame;
    QList<QByteArray> dataLines;
    QList<QByteArray> comments;
    QByteArray normalized = block;
    normalized.replace(QByteArrayLiteral("\r\n"), QByteArrayLiteral("\n"));
    normalized.replace('\r', '\n');
    const QList<QByteArray> lines = normalized.split('\n');
    for (const QByteArray &line : lines) {
        if (line.startsWith(':')) {
            comments.append(line.mid(1).trimmed());
            continue;
        }
        const qsizetype separator = line.indexOf(':');
        const QByteArray field = separator < 0 ? line : line.left(separator);
        QByteArray value = separator < 0 ? QByteArray{} : line.mid(separator + 1);
        if (value.startsWith(' ')) {
            value.remove(0, 1);
        }
        if (field == QByteArrayLiteral("event")) {
            frame.event = value;
        } else if (field == QByteArrayLiteral("id") && !value.contains('\0')) {
            frame.id = value;
        } else if (field == QByteArrayLiteral("data")) {
            dataLines.append(value);
        } else if (field == QByteArrayLiteral("retry")) {
            bool ok = false;
            const qint64 retry = value.toLongLong(&ok);
            if (ok && retry >= 0) {
                frame.retryMillis = retry;
            }
        }
    }
    for (const QByteArray &line : std::as_const(dataLines)) {
        if (!frame.data.isEmpty()) {
            frame.data.append('\n');
        }
        frame.data.append(line);
    }
    for (const QByteArray &line : std::as_const(comments)) {
        if (!frame.comment.isEmpty()) {
            frame.comment.append('\n');
        }
        frame.comment.append(line);
    }
    return frame;
}

SyncClient::SyncClient(QUrl baseUrl, QByteArray bearerToken, QObject *parent)
    : QObject(parent)
    , m_baseUrl(std::move(baseUrl))
    , m_bearerToken(std::move(bearerToken))
{
    // Every request carries the device token: a redirect is followed only to the same
    // scheme, host and port, so the token never reaches another origin.
    m_network.setRedirectPolicy(QNetworkRequest::SameOriginRedirectPolicy);
}

void SyncClient::setTraceId(const QByteArray &traceId)
{
    m_traceId = traceId;
}

QByteArray SyncClient::traceId() const
{
    return m_traceId;
}

QNetworkReply *SyncClient::ready()
{
    return get(request(QStringLiteral("/v1/ready")));
}

QNetworkReply *SyncClient::deviceSequence()
{
    return get(request(QStringLiteral("/v1/device-sequence")));
}

QNetworkRequest SyncClient::request(const QString &path) const
{
    QUrl url = m_baseUrl;
    url.setPath(m_baseUrl.path().chopped(m_baseUrl.path().endsWith(QLatin1Char('/')) ? 1 : 0) + path);
    QNetworkRequest request(url);
    request.setRawHeader(QByteArrayLiteral("Authorization"), QByteArrayLiteral("Bearer ") + m_bearerToken);
    request.setRawHeader(QByteArrayLiteral("Accept"), QByteArrayLiteral("application/json"));
    if (!m_traceId.isEmpty()) {
        request.setRawHeader(QByteArrayLiteral("X-Thoughts-Trace-Id"), m_traceId);
    }
#ifdef Q_OS_WIN
    // Qt's HTTP/2 on Windows got COMPRESSION_ERROR from the connection ("Server is
    // unable to maintain the header compression context"); HTTP/1.1 works there.
    request.setAttribute(QNetworkRequest::Http2AllowedAttribute, false);
#endif
    request.setTransferTimeout(30000);
    return request;
}

QNetworkReply *SyncClient::postJson(const QString &path, const QJsonObject &payload)
{
    QNetworkRequest outgoing = request(path);
    outgoing.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
    return post(outgoing, QJsonDocument(payload).toJson(QJsonDocument::Compact));
}

QNetworkReply *SyncClient::get(const QNetworkRequest &outgoing)
{
    return withConnectionTimeout(m_network.get(outgoing));
}

QNetworkReply *SyncClient::post(const QNetworkRequest &outgoing, const QByteArray &content)
{
    return withConnectionTimeout(m_network.post(outgoing, content));
}

QNetworkReply *SyncClient::withConnectionTimeout(QNetworkReply *reply)
{
    auto *timer = new QTimer(reply);
    timer->setSingleShot(true);
    timer->setInterval(10000);
    connect(reply, &QNetworkReply::requestSent, timer, &QTimer::stop);
    connect(reply, &QNetworkReply::finished, timer, &QTimer::stop);
    connect(timer, &QTimer::timeout, reply, &QNetworkReply::abort);
    timer->start();
    return reply;
}

QNetworkReply *SyncClient::uploadOperations(const QList<QJsonObject> &operations)
{
    QJsonArray values;
    for (const QJsonObject &operation : operations) {
        values.append(operation);
    }
    return postJson(QStringLiteral("/v1/operations"), QJsonObject{{QStringLiteral("operations"), values}});
}

QNetworkReply *SyncClient::anchorJob(const QString &jobId)
{
    return get(request(QStringLiteral("/v1/anchor-jobs/") + jobId));
}

QNetworkReply *SyncClient::changes(qint64 after, int limit)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/changes"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("after"), QString::number(after));
    query.addQueryItem(QStringLiteral("limit"), QString::number(limit));
    url.setQuery(query);
    outgoing.setUrl(url);
    return get(outgoing);
}

QNetworkReply *SyncClient::projections()
{
    return get(request(QStringLiteral("/v1/projections")));
}

QNetworkReply *SyncClient::documentSync(const QString &target, const QByteArray &ifNoneMatch)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/document-sync"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("target"), target);
    url.setQuery(query);
    outgoing.setUrl(url);
    if (!ifNoneMatch.isEmpty()) {
        outgoing.setRawHeader("If-None-Match", QByteArrayLiteral("\"") + ifNoneMatch + QByteArrayLiteral("\""));
    }
    return get(outgoing);
}

QNetworkReply *SyncClient::syncState()
{
    return get(request(QStringLiteral("/v2/sync/state")));
}

QNetworkReply *SyncClient::syncPlan(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v2/sync/plan"), payload);
}

QNetworkReply *SyncClient::library()
{
    return get(request(QStringLiteral("/v2/library")));
}

QNetworkReply *SyncClient::sourceBundleObject(const QByteArray &digest, const QByteArray &syncPlanId)
{
    if (syncPlanId.isEmpty()) {
        return nullptr;
    }
    QNetworkRequest outgoing = request(QStringLiteral("/v2/artifacts/") + QString::fromLatin1(digest));
    outgoing.setTransferTimeout(60000);
    outgoing.setRawHeader(QByteArrayLiteral("X-Thoughts-Sync-Plan"), syncPlanId);
    return get(outgoing);
}

QNetworkReply *SyncClient::importLibraryPdf(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/library/imports"), payload);
}

QNetworkReply *SyncClient::providerProjects()
{
    return get(request(QStringLiteral("/v1/provider-projects")));
}

QNetworkReply *SyncClient::libraryProjects()
{
    return get(request(QStringLiteral("/v1/library/projects")));
}

QNetworkReply *SyncClient::requestProjectExport(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v2/library/project-exports"), payload);
}

QNetworkReply *SyncClient::projectExport(const QString &id)
{
    return get(request(QStringLiteral("/v2/library/project-exports/") + QString::fromLatin1(QUrl::toPercentEncoding(id))));
}

QNetworkReply *SyncClient::editLibraryProject(const QJsonObject &payload)
{
    if (payload.value(QStringLiteral("action")).toString() == QLatin1String("import_folder")) {
        auto outgoing = request(QStringLiteral("/v1/library/projects"));
        outgoing.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
        outgoing.setTransferTimeout(900000);
        return withConnectionTimeout(m_network.post(outgoing, QJsonDocument(payload).toJson(QJsonDocument::Compact)));
    }
    return postJson(QStringLiteral("/v1/library/projects"), payload);
}

QNetworkReply *SyncClient::setLibraryDisplayName(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/library/display-names"), payload);
}

QNetworkReply *SyncClient::setLibraryArchiveState(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/library/archive-state"), payload);
}

QNetworkReply *SyncClient::removeLibraryPdf(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/library/removals"), payload);
}

QNetworkReply *SyncClient::resolveSourceSelection(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/source-selections/resolve"), payload);
}

QNetworkReply *SyncClient::objectMetadata(const QByteArray &digest)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/objects/") + QString::fromLatin1(digest));
    outgoing.setTransferTimeout(60000);
    return withConnectionTimeout(m_network.head(outgoing));
}

QNetworkReply *SyncClient::uploadObject(const QByteArray &content, const QByteArray &mediaType)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/objects"));
    outgoing.setTransferTimeout(60000);
    outgoing.setRawHeader(QByteArrayLiteral("Content-Type"), mediaType);
    outgoing.setRawHeader(QByteArrayLiteral("X-Content-SHA256"), sha256(content));
    return post(outgoing, content);
}

QNetworkReply *SyncClient::uploadObject(QIODevice *content, const QByteArray &digest, qint64 size, const QByteArray &mediaType)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/objects"));
    outgoing.setTransferTimeout(60000);
    outgoing.setRawHeader(QByteArrayLiteral("Content-Type"), mediaType);
    outgoing.setRawHeader(QByteArrayLiteral("X-Content-SHA256"), digest);
    outgoing.setHeader(QNetworkRequest::ContentLengthHeader, size);
    return withConnectionTimeout(m_network.post(outgoing, content));
}

QNetworkReply *SyncClient::downloadObject(const QByteArray &digest, const QByteArray &syncPlanId)
{
    const QString endpoint = syncPlanId.isEmpty()
        ? QStringLiteral("/v1/objects/")
        : QStringLiteral("/v2/artifacts/");
    QNetworkRequest outgoing = request(endpoint + QString::fromLatin1(digest));
    outgoing.setTransferTimeout(60000);
    if (!syncPlanId.isEmpty()) {
        outgoing.setRawHeader(QByteArrayLiteral("X-Thoughts-Sync-Plan"), syncPlanId);
    }
    return get(outgoing);
}

QNetworkReply *SyncClient::applySource(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/sources/apply"), payload);
}

QNetworkReply *SyncClient::sources()
{
    return get(request(QStringLiteral("/v1/sources")));
}

QNetworkReply *SyncClient::publishSources(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/source-publications"), payload);
}

QNetworkReply *SyncClient::resolveAgentProposals(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v2/agent-proposal-resolutions"), payload);
}

QNetworkReply *SyncClient::agentProposalPreview(const QString &artifactSha256,
                                                const QString &proposalId)
{
    QNetworkRequest outgoing = request(
        QStringLiteral("/v2/agent-proposal-previews/")
        + artifactSha256 + QLatin1Char('/') + proposalId);
    outgoing.setTransferTimeout(300000);
    return get(outgoing);
}

QNetworkReply *SyncClient::agentProposalResolution(const QString &operationId)
{
    return get(request(QStringLiteral("/v2/agent-proposal-resolutions/") + operationId));
}

QNetworkReply *SyncClient::undoAgentProposalResolution(const QString &operationId,
                                                       const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v2/agent-proposal-resolutions/") + operationId + QStringLiteral("/undo"), payload);
}

QNetworkReply *SyncClient::publishBuildInputs(const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/build-input-snapshots"), payload);
}

QNetworkReply *SyncClient::latestBuildInputs(const QString &target)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/build-input-snapshots/latest"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("target"), target);
    url.setQuery(query);
    outgoing.setUrl(url);
    return get(outgoing);
}

QNetworkReply *SyncClient::resolveConflict(const QString &conflictId, const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/conflicts/") + conflictId + QStringLiteral("/resolve"), payload);
}

QNetworkReply *SyncClient::discardConflict(const QString &conflictId, const QJsonObject &payload)
{
    return postJson(QStringLiteral("/v1/conflicts/") + conflictId + QStringLiteral("/discard"), payload);
}

QNetworkReply *SyncClient::conflicts(const QString &status)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/conflicts"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("status"), status);
    url.setQuery(query);
    outgoing.setUrl(url);
    return get(outgoing);
}

QNetworkReply *SyncClient::requestBuild(const QString &target,
                                        const QString &requestId,
                                        qint64 sourceFrontier,
                                        const QString &inputSnapshotSha256)
{
    QJsonObject scope{{QStringLiteral("kind"), target == QLatin1String("all") ? QStringLiteral("library") : QStringLiteral("document")}};
    if (target != QLatin1String("all")) {
        scope.insert(QStringLiteral("filename"), target);
    }
    QJsonObject payload{{QStringLiteral("scope"), scope}, {QStringLiteral("mode"), QStringLiteral("force")}};
    if (!requestId.isEmpty()) {
        payload.insert(QStringLiteral("request_id"), requestId);
    }
    if (sourceFrontier >= 0) {
        payload.insert(QStringLiteral("source_frontier"), sourceFrontier);
    }
    if (!inputSnapshotSha256.isEmpty()) {
        payload.insert(QStringLiteral("input_snapshot_sha256"), inputSnapshotSha256);
    }
    return postJson(QStringLiteral("/v2/builds"), payload);
}

QNetworkReply *SyncClient::renderCheck(const QString &target, const QString &requestId)
{
    QJsonObject payload{{QStringLiteral("target"), target}};
    if (!requestId.isEmpty()) {
        payload.insert(QStringLiteral("request_id"), requestId);
    }
    return postJson(QStringLiteral("/v1/render-checks"), payload);
}

QNetworkReply *SyncClient::renderStatus(const QString &target)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/render-status"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("target"), target);
    url.setQuery(query);
    outgoing.setUrl(url);
    return get(outgoing);
}

QNetworkReply *SyncClient::renderState(const QString &target)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v1/render-state"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("target"), target);
    url.setQuery(query);
    outgoing.setUrl(url);
    return get(outgoing);
}

QNetworkReply *SyncClient::build(const QString &jobId, int waitMs)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v2/builds/") + jobId);
    outgoing.setTransferTimeout(60000);
    if (waitMs > 0) {
        QUrl url = outgoing.url();
        QUrlQuery query;
        query.addQueryItem(QStringLiteral("wait_ms"), QString::number(qBound(0, waitMs, 30000)));
        url.setQuery(query);
        outgoing.setUrl(url);
    }
    return get(outgoing);
}

QNetworkReply *SyncClient::cancelBuild(const QString &jobId)
{
    return postJson(QStringLiteral("/v1/builds/%1/cancel").arg(jobId), {});
}

QNetworkReply *SyncClient::buildOutput(const QString &jobId, qint64 offset)
{
    return get(request(QStringLiteral("/v2/builds/%1/events?cursor=%2").arg(jobId).arg(qMax<qint64>(0, offset))));
}

QNetworkReply *SyncClient::traceSnapshot(const QString &traceId)
{
    return get(request(QStringLiteral("/v2/traces/") + traceId));
}

QNetworkReply *SyncClient::traceEvents(const QString &traceId, qint64 after, int limit)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v2/traces/") + traceId + QStringLiteral("/events"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("after"), QString::number(qMax<qint64>(0, after)));
    query.addQueryItem(QStringLiteral("limit"), QString::number(qBound(1, limit, 500)));
    url.setQuery(query);
    outgoing.setUrl(url);
    return get(outgoing);
}

QNetworkReply *SyncClient::traceStream(const QString &traceId, qint64 after)
{
    QNetworkRequest outgoing = request(QStringLiteral("/v2/traces/") + traceId + QStringLiteral("/stream"));
    QUrl url = outgoing.url();
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("after"), QString::number(qMax<qint64>(0, after)));
    url.setQuery(query);
    outgoing.setUrl(url);
    outgoing.setRawHeader(QByteArrayLiteral("Accept"), QByteArrayLiteral("text/event-stream"));
    outgoing.setRawHeader(QByteArrayLiteral("Accept-Encoding"), QByteArrayLiteral("identity"));
    outgoing.setRawHeader(QByteArrayLiteral("Cache-Control"), QByteArrayLiteral("no-cache"));
    outgoing.setRawHeader(QByteArrayLiteral("Last-Event-ID"), QByteArray::number(qMax<qint64>(0, after)));
    outgoing.setTransferTimeout(0);
    return get(outgoing);
}

QByteArray SyncClient::sha256(const QByteArray &content)
{
#ifdef Q_OS_MACOS
    // CommonCrypto uses the platform SHA instructions; keep the exact wire digest.
    CC_SHA256_CTX context;
    if (CC_SHA256_Init(&context) != 1) return {};
    for (qsizetype offset = 0; offset < content.size();) {
        const auto count = static_cast<CC_LONG>(qMin<qsizetype>(content.size() - offset, 1024 * 1024));
        if (CC_SHA256_Update(&context, content.constData() + offset, count) != 1) return {};
        offset += count;
    }
    unsigned char digest[CC_SHA256_DIGEST_LENGTH];
    if (CC_SHA256_Final(digest, &context) != 1) return {};
    return QByteArray(reinterpret_cast<const char *>(digest), sizeof(digest)).toHex();
#else
    return QCryptographicHash::hash(content, QCryptographicHash::Sha256).toHex();
#endif
}

bool SyncClient::verifyObject(const QByteArray &content, const QByteArray &expectedSha256)
{
    return sha256(content) == expectedSha256.toLower();
}

QList<QList<QJsonObject>> SyncClient::operationBatches(const QList<QJsonObject> &operations,
                                                       qsizetype batchSize)
{
    QList<QList<QJsonObject>> batches;
    if (batchSize <= 0) {
        return batches;
    }
    for (qsizetype offset = 0; offset < operations.size(); offset += batchSize) {
        batches.append(operations.mid(offset, batchSize));
    }
    return batches;
}

QJsonObject SyncClient::normalizedAnnotationPayload(QJsonObject payload)
{
    const QJsonValue subtype = payload.value(QStringLiteral("subtype"));
    if (subtype.isUndefined() || (subtype.isString() && subtype.toString().trimmed().isEmpty())) {
        payload.insert(QStringLiteral("subtype"), QStringLiteral("Text"));
    }
    return payload;
}

QList<QJsonObject> SyncClient::normalizedOperationsForUpload(const QList<QJsonObject> &operations)
{
    QList<QJsonObject> normalized;
    normalized.reserve(operations.size());
    for (QJsonObject operation : operations) {
        if (operation.value(QStringLiteral("entity_type")).toString() == QLatin1String("annotation")) {
            operation.insert(QStringLiteral("payload"), normalizedAnnotationPayload(operation.value(QStringLiteral("payload")).toObject()));
        }
        normalized.append(operation);
    }
    return normalized;
}

SyncErrorInfo SyncClient::parseErrorResponse(const QByteArray &body,
                                             const QString &fallback,
                                             int httpStatus,
                                             const QList<QJsonObject> &operations)
{
    SyncErrorInfo result;
    result.httpStatus = httpStatus;
    const QJsonDocument document = QJsonDocument::fromJson(body);
    const QJsonValue detail = document.isObject() ? document.object().value(QStringLiteral("detail")) : QJsonValue();

    if (detail.isArray()) {
        QStringList detailBlocks;
        QSet<int> invalidOperationIndexes;
        bool emptySubtype = false;
        for (const QJsonValue &value : detail.toArray()) {
            const QJsonObject validation = value.toObject();
            const QJsonArray location = validation.value(QStringLiteral("loc")).toArray();
            int operationIndex = -1;
            QStringList fieldParts;
            bool afterOperations = false;
            for (const QJsonValue &component : location) {
                if (component.isString()) {
                    const QString text = component.toString();
                    if (text == QLatin1String("body")) {
                        continue;
                    }
                    if (text == QLatin1String("operations")) {
                        afterOperations = true;
                        continue;
                    }
                    if (afterOperations) {
                        fieldParts.append(text);
                    }
                } else if (afterOperations && component.isDouble() && operationIndex < 0) {
                    operationIndex = component.toInt(-1);
                }
            }
            if (operationIndex >= 0) {
                invalidOperationIndexes.insert(operationIndex);
            }
            if (!fieldParts.isEmpty() && fieldParts.constLast() == QLatin1String("subtype")
                && validation.value(QStringLiteral("type")).toString() == QLatin1String("string_too_short")) {
                emptySubtype = true;
            }

            QStringList lines;
            if (httpStatus > 0) {
                lines.append(QStringLiteral("Server HTTP %1").arg(httpStatus));
            }
            if (operationIndex >= 0) {
                lines.append(QStringLiteral("Queued change %1 of %2").arg(operationIndex + 1).arg(operations.size()));
                if (operationIndex < operations.size()) {
                    const QJsonObject operation = operations.at(operationIndex);
                    const QString collection = operation.value(QStringLiteral("collection")).toString();
                    const QString operationId = operation.value(QStringLiteral("operation_id")).toString();
                    if (!collection.isEmpty()) {
                        lines.append(QStringLiteral("Collection: %1").arg(collection));
                    }
                    if (!operationId.isEmpty()) {
                        lines.append(QStringLiteral("Operation ID: %1").arg(operationId));
                    }
                }
            }
            if (!fieldParts.isEmpty()) {
                QString field = fieldParts.join(QLatin1Char('.'));
                if (!field.startsWith(QLatin1String("payload."))) {
                    field.prepend(QStringLiteral("payload."));
                }
                lines.append(QStringLiteral("Field: %1").arg(field));
            }
            const QString message = validation.value(QStringLiteral("msg")).toString();
            if (!message.isEmpty()) {
                lines.append(QStringLiteral("Validation: %1").arg(message));
            }
            if (!lines.isEmpty()) {
                detailBlocks.append(lines.join(QLatin1Char('\n')));
            }
        }

        if (emptySubtype) {
            result.summary = QStringLiteral("A queued annotation has an empty subtype. Your changes remain safely queued.");
        } else if (invalidOperationIndexes.size() == 1) {
            result.summary = QStringLiteral("The server rejected a queued change. Your changes remain safely queued.");
        } else if (invalidOperationIndexes.size() > 1) {
            result.summary = QStringLiteral("The server rejected %1 queued changes. Your changes remain safely queued.").arg(invalidOperationIndexes.size());
        } else {
            result.summary = QStringLiteral("The server rejected the sync request. Your changes remain safely queued.");
        }
        result.details = detailBlocks.join(QStringLiteral("\n\n"));
        if (result.details.isEmpty()) {
            result.details = fallback;
        }
        return result;
    }

    if (detail.isString() && !detail.toString().trimmed().isEmpty()) {
        result.summary = detail.toString().trimmed();
        result.details = httpStatus > 0
            ? QStringLiteral("Server HTTP %1\n%2").arg(httpStatus).arg(result.summary)
            : result.summary;
        return result;
    }

    if (httpStatus > 0) {
        result.summary = QStringLiteral("The server rejected the sync request (HTTP %1). Your changes remain safely queued.").arg(httpStatus);
        result.details = fallback.isEmpty() ? result.summary : fallback;
    } else {
        result.summary = QStringLiteral("Could not reach the server. Your changes remain safely queued.");
        result.details = fallback.isEmpty() ? result.summary : fallback;
    }
    return result;
}

}

QNetworkReply *Thoughts::SyncClient::buildSourceDiff(const QString &buildId)
{
    return get(request(QStringLiteral("/v1/builds/") + buildId + QStringLiteral("/source-diff")));
}
