// SPDX-License-Identifier: GPL-2.0-or-later
#pragma once

#include <QByteArray>
#include <QJsonObject>
#include <QList>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QObject>
#include <QUrl>

namespace Thoughts
{

inline constexpr auto SourceResourceCapability = "source_resource_v1";
// Review cards made inside HTML attachment pages: primary created_in "html_attachment",
// source_link state "html_attachment" (server docs/HTML_ATTACHMENTS.md).
inline constexpr auto HtmlAttachmentCardsCapability = "html_attachment_cards_v1";
inline constexpr auto HtmlAttachmentUnknownError = "html_attachment_unknown";

struct SyncErrorInfo {
    QString summary;
    QString details;
    int httpStatus = 0;
};

struct TraceSseFrame {
    QByteArray event;
    QByteArray id;
    QByteArray data;
    qint64 retryMillis = -1;
    QByteArray comment;
};

class TraceSseParser
{
public:
    QList<TraceSseFrame> feed(const QByteArray &chunk);
    QList<TraceSseFrame> finish();

private:
    QList<TraceSseFrame> drain(bool complete);
    static TraceSseFrame parseBlock(const QByteArray &block);

    QByteArray m_pending;
};

class SyncClient : public QObject
{
    Q_OBJECT

public:
    explicit SyncClient(QUrl baseUrl, QByteArray bearerToken, QObject *parent = nullptr);

    void setTraceId(const QByteArray &traceId);
    QByteArray traceId() const;

    QNetworkReply *ready();
    QNetworkReply *deviceSequence();
    QNetworkReply *uploadOperations(const QList<QJsonObject> &operations);
    QNetworkReply *anchorJob(const QString &jobId);
    QNetworkReply *changes(qint64 after, int limit = 200);
    QNetworkReply *projections();
    QNetworkReply *documentSync(const QString &target, const QByteArray &ifNoneMatch = {});
    QNetworkReply *syncState();
    QNetworkReply *syncPlan(const QJsonObject &request);
    QNetworkReply *library();
    QNetworkReply *libraryProjects();
    QNetworkReply *requestProjectExport(const QJsonObject &request);
    QNetworkReply *projectExport(const QString &id);
    QNetworkReply *providerProjects();
    QNetworkReply *editLibraryProject(const QJsonObject &request);
    QNetworkReply *sourceBundleObject(const QByteArray &sha256, const QByteArray &syncPlanId);
    QNetworkReply *importLibraryPdf(const QJsonObject &request);
    QNetworkReply *setLibraryDisplayName(const QJsonObject &request);
    QNetworkReply *setLibraryArchiveState(const QJsonObject &request);
    QNetworkReply *removeLibraryPdf(const QJsonObject &request);
    QNetworkReply *resolveSourceSelection(const QJsonObject &request);
    QNetworkReply *objectMetadata(const QByteArray &sha256);
    QNetworkReply *uploadObject(const QByteArray &content, const QByteArray &mediaType);
    QNetworkReply *uploadObject(QIODevice *content, const QByteArray &digest, qint64 size, const QByteArray &mediaType);
    QNetworkReply *downloadObject(const QByteArray &sha256, const QByteArray &syncPlanId = {});
    QNetworkReply *applySource(const QJsonObject &request);
    QNetworkReply *sources();
    QNetworkReply *publishSources(const QJsonObject &request);
    QNetworkReply *resolveAgentProposals(const QJsonObject &request);
    QNetworkReply *agentProposalPreview(const QString &artifactSha256,
                                        const QString &proposalId);
    QNetworkReply *agentProposalResolution(const QString &operationId);
    QNetworkReply *undoAgentProposalResolution(const QString &operationId,
                                               const QJsonObject &request);
    QNetworkReply *publishBuildInputs(const QJsonObject &request);
    QNetworkReply *latestBuildInputs(const QString &target);
    QNetworkReply *buildSourceDiff(const QString &buildId);
    QNetworkReply *resolveConflict(const QString &conflictId, const QJsonObject &request);
    QNetworkReply *discardConflict(const QString &conflictId, const QJsonObject &request);
    QNetworkReply *conflicts(const QString &status = QStringLiteral("unresolved"));
    QNetworkReply *requestBuild(const QString &target,
                                const QString &requestId = {},
                                qint64 sourceFrontier = -1,
                                const QString &inputSnapshotSha256 = {});
    QNetworkReply *renderCheck(const QString &target = QStringLiteral("all"),
                               const QString &requestId = {});
    QNetworkReply *renderStatus(const QString &target);
    QNetworkReply *renderState(const QString &target = QStringLiteral("all"));
    QNetworkReply *build(const QString &jobId, int waitMs = 0);
    QNetworkReply *cancelBuild(const QString &jobId);
    QNetworkReply *buildOutput(const QString &jobId, qint64 offset = 0);
    QNetworkReply *traceSnapshot(const QString &traceId);
    QNetworkReply *traceEvents(const QString &traceId, qint64 after = 0, int limit = 500);
    QNetworkReply *traceStream(const QString &traceId, qint64 after = 0);

    static QByteArray sha256(const QByteArray &content);
    static bool verifyObject(const QByteArray &content, const QByteArray &expectedSha256);
    static QList<QList<QJsonObject>> operationBatches(const QList<QJsonObject> &operations,
                                                      qsizetype batchSize = 200);
    static QJsonObject normalizedAnnotationPayload(QJsonObject payload);
    static QList<QJsonObject> normalizedOperationsForUpload(const QList<QJsonObject> &operations);
    static SyncErrorInfo parseErrorResponse(const QByteArray &body,
                                            const QString &fallback,
                                            int httpStatus,
                                            const QList<QJsonObject> &operations = {});

private:
    QNetworkRequest request(const QString &path) const;
    QNetworkReply *get(const QNetworkRequest &request);
    QNetworkReply *post(const QNetworkRequest &request, const QByteArray &content);
    QNetworkReply *withConnectionTimeout(QNetworkReply *reply);
    QNetworkReply *postJson(const QString &path, const QJsonObject &payload);

    QUrl m_baseUrl;
    QByteArray m_bearerToken;
    QByteArray m_traceId;
    QNetworkAccessManager m_network;
};

}
