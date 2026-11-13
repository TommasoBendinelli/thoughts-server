// SPDX-License-Identifier: GPL-2.0-or-later
#include "thoughts/durableoutbox.h"
#include "thoughts/syncclient.h"
#include "thoughts/synccore.h"

#include <QJsonObject>
#include <QCryptographicHash>
#include <QJsonArray>
#include <QFile>
#include <QHostAddress>
#include <QJsonDocument>
#include <QNetworkReply>
#include <QSignalSpy>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTemporaryDir>
#include <QTest>

static bool completeHttpRequest(const QByteArray &request);

class ClientTest : public QObject
{
    Q_OBJECT

private Q_SLOTS:
    void durableOutbox();
    void durableAgentProposalRequests();
    void repairsBlankAnnotationSubtypes();
    void malformedOutboxIsLeftUntouched();
    void objectHash();
    void operationBatching();
    void annotationSubtypeNormalization();
    void structuredErrorDetails();
    void syncStateRequest();
    void redirectToAnotherOriginIsNotFollowed();
    void objectMetadataRequest();
    void plannedObjectRequest_data();
    void plannedObjectRequest();
    void unplannedObjectRequestUsesGenericRoute();
    void renderCheckRequest_data();
    void renderCheckRequest();
    void discardConflictRequest();
    void sharedReconcilerTransitions();
    void sharedSyncPolicy();
    void sharedTraceSseFixture();
    void sharedSyncTransferPlanFixture();
};

void ClientTest::sharedSyncPolicy()
{
    QFile file(QStringLiteral(THOUGHTS_PROTOCOL_FIXTURE_DIR "/sync-policy.json"));
    QVERIFY(file.open(QIODevice::ReadOnly));
    const QJsonObject fixture = QJsonDocument::fromJson(file.readAll()).object();
    for (const QJsonValue &value : fixture.value(QStringLiteral("cases")).toArray()) {
        const QJsonObject item = value.toObject();
        const QJsonObject input = item.value(QStringLiteral("facts")).toObject();
        QHash<QString, bool> facts;
        for (auto fact = input.constBegin(); fact != input.constEnd(); ++fact) facts.insert(fact.key(), fact.value().toBool());
        const auto result = item.value(QStringLiteral("decision")).toString() == QLatin1String("sourceDecision") ? Thoughts::SyncCore::sourceDecision(facts) : Thoughts::SyncCore::nextAction(facts);
        QCOMPARE(result.action, item.value(QStringLiteral("action")).toString());
        QCOMPARE(result.reason, item.value(QStringLiteral("reason")).toString());
    }
}

void ClientTest::durableOutbox()
{
    QTemporaryDir directory;
    QVERIFY(directory.isValid());
    Thoughts::DurableOutbox outbox(directory.filePath(QStringLiteral("outbox.json")));
    const QJsonObject operation = outbox.enqueue(QStringLiteral("comment"),
                                                 QStringLiteral("comment-1"),
                                                 QStringLiteral("manual"),
                                                 -1,
                                                 QJsonObject{{QStringLiteral("id"), QStringLiteral("comment-1")}},
                                                 QStringLiteral("2026-08-11T12:00:00Z"));
    QVERIFY(!operation.isEmpty());
    QCOMPARE(outbox.operations().size(), 1);
    QVERIFY(outbox.advanceCursor(0, 7));
    QCOMPARE(outbox.cursor(), 7);
    QVERIFY(outbox.acknowledge({operation.value(QStringLiteral("operation_id")).toString()}));
    QVERIFY(outbox.operations().isEmpty());
}

void ClientTest::durableAgentProposalRequests()
{
    QTemporaryDir directory;
    QVERIFY(directory.isValid());
    Thoughts::DurableOutbox outbox(directory.filePath(QStringLiteral("outbox.json")));
    const QString proposalId = QStringLiteral("agent-remove-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    const QJsonObject resolution = outbox.enqueueAgentProposalResolution(
        QString(64, QLatin1Char('a')),
        QStringLiteral("accept"),
        QJsonArray{QJsonObject{{QStringLiteral("id"), proposalId},
                               {QStringLiteral("object_sha256"), QString(64, QLatin1Char('b'))}}});
    QCOMPARE(resolution.value(QStringLiteral("outbox_kind")).toString(), QStringLiteral("agent_proposal_resolution"));
    QCOMPARE(resolution.value(QStringLiteral("device_sequence")).toInteger(), 1);
    QCOMPARE(
        resolution.value(QStringLiteral("proposals")).toArray().first().toObject().value(QStringLiteral("id")).toString(),
        proposalId);
    const QJsonObject undo = outbox.enqueueAgentProposalUndo(resolution.value(QStringLiteral("operation_id")).toString());
    QCOMPARE(undo.value(QStringLiteral("outbox_kind")).toString(), QStringLiteral("agent_proposal_undo"));
    QCOMPARE(undo.value(QStringLiteral("device_sequence")).toInteger(), 2);
    QCOMPARE(outbox.operations().size(), 2);
}

void ClientTest::repairsBlankAnnotationSubtypes()
{
    QTemporaryDir directory;
    QVERIFY(directory.isValid());
    const QString path = directory.filePath(QStringLiteral("outbox.json"));
    Thoughts::DurableOutbox outbox(path);
    const QJsonObject blank = outbox.enqueue(QStringLiteral("annotation"),
                                             QStringLiteral("blank"),
                                             QStringLiteral("paper"),
                                             0,
                                             QJsonObject{{QStringLiteral("subtype"), QStringLiteral("   ")}, {QStringLiteral("deleted"), true}},
                                             QStringLiteral("2026-08-17T10:00:00Z"));
    const QJsonObject highlight = outbox.enqueue(QStringLiteral("annotation"),
                                                 QStringLiteral("highlight"),
                                                 QStringLiteral("paper"),
                                                 0,
                                                 QJsonObject{{QStringLiteral("subtype"), QStringLiteral("Highlight")}},
                                                 QStringLiteral("2026-08-17T10:00:01Z"));
    const QJsonObject missing = outbox.enqueue(QStringLiteral("annotation"),
                                               QStringLiteral("missing"),
                                               QStringLiteral("paper"),
                                               0,
                                               QJsonObject(),
                                               QStringLiteral("2026-08-17T10:00:02Z"));
    QVERIFY(!blank.isEmpty());
    QVERIFY(!highlight.isEmpty());
    QVERIFY(!missing.isEmpty());
    QVERIFY(outbox.advanceCursor(0, 91));

    QFile originalFile(path);
    QVERIFY(originalFile.open(QIODevice::ReadOnly));
    const QByteArray original = originalFile.readAll();
    originalFile.close();

    const Thoughts::AnnotationSubtypeRepairResult repaired = outbox.repairBlankAnnotationSubtypes();
    QVERIFY2(repaired.success, qPrintable(repaired.error));
    QCOMPARE(repaired.repaired, 1);
    QVERIFY(!repaired.backupPath.isEmpty());
    QFile backup(repaired.backupPath);
    QVERIFY(backup.open(QIODevice::ReadOnly));
    QCOMPARE(backup.readAll(), original);
    QCOMPARE(repaired.backupSha256, Thoughts::SyncClient::sha256(original));

    const QList<QJsonObject> operations = outbox.operations();
    QCOMPARE(operations.size(), 3);
    QCOMPARE(operations.at(0).value(QStringLiteral("operation_id")), blank.value(QStringLiteral("operation_id")));
    QCOMPARE(operations.at(0).value(QStringLiteral("device_sequence")), blank.value(QStringLiteral("device_sequence")));
    QCOMPARE(operations.at(0).value(QStringLiteral("payload")).toObject().value(QStringLiteral("subtype")).toString(), QStringLiteral("Text"));
    QCOMPARE(operations.at(1).value(QStringLiteral("payload")).toObject().value(QStringLiteral("subtype")).toString(), QStringLiteral("Highlight"));
    QVERIFY(!operations.at(2).value(QStringLiteral("payload")).toObject().contains(QStringLiteral("subtype")));
    QCOMPARE(outbox.cursor(), 91);

    const Thoughts::AnnotationSubtypeRepairResult second = outbox.repairBlankAnnotationSubtypes();
    QVERIFY(second.success);
    QCOMPARE(second.repaired, 0);
    QVERIFY(second.backupPath.isEmpty());
}

void ClientTest::malformedOutboxIsLeftUntouched()
{
    QTemporaryDir directory;
    QVERIFY(directory.isValid());
    const QString path = directory.filePath(QStringLiteral("outbox.json"));
    const QByteArray malformed = QByteArrayLiteral("{not-json\n");
    QFile file(path);
    QVERIFY(file.open(QIODevice::WriteOnly));
    QCOMPARE(file.write(malformed), malformed.size());
    file.close();

    Thoughts::DurableOutbox outbox(path);
    const Thoughts::AnnotationSubtypeRepairResult result = outbox.repairBlankAnnotationSubtypes();
    QVERIFY(!result.success);
    QCOMPARE(result.repaired, 0);
    QVERIFY(result.backupPath.isEmpty());
    QVERIFY(file.open(QIODevice::ReadOnly));
    QCOMPARE(file.readAll(), malformed);
}

void ClientTest::objectHash()
{
    for (const QByteArray &raw : {QByteArray(), QByteArray(3 * 1024 * 1024 + 7, '\0')}) {
        const QByteArray digest = QCryptographicHash::hash(raw, QCryptographicHash::Sha256).toHex();
        QCOMPARE(Thoughts::SyncClient::sha256(raw), digest);
        QVERIFY(Thoughts::SyncClient::verifyObject(raw, digest.toUpper()));
        QVERIFY(!Thoughts::SyncClient::verifyObject(raw + "x", digest));
    }

    QCOMPARE(Thoughts::SyncClient::sha256("hello"),
             QByteArray("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"));
    Thoughts::SyncClient client(QUrl(QStringLiteral("https://thoughts.example")), QByteArrayLiteral("token"));
    client.setTraceId(QByteArrayLiteral("11111111-1111-4111-8111-111111111111"));
    QCOMPARE(client.traceId(), QByteArrayLiteral("11111111-1111-4111-8111-111111111111"));
}

void ClientTest::operationBatching()
{
    QList<QJsonObject> operations;
    for (int index = 0; index < 617; ++index) {
        operations.append(QJsonObject{{QStringLiteral("index"), index}});
    }
    const auto batches = Thoughts::SyncClient::operationBatches(operations);
    QCOMPARE(batches.size(), 4);
    QCOMPARE(batches.at(0).size(), 200);
    QCOMPARE(batches.at(3).size(), 17);
}

void ClientTest::annotationSubtypeNormalization()
{
    const QList<QJsonObject> operations {
        QJsonObject{{QStringLiteral("entity_type"), QStringLiteral("annotation")},
                    {QStringLiteral("payload"), QJsonObject{{QStringLiteral("subtype"), QStringLiteral("  ")}}}},
        QJsonObject{{QStringLiteral("entity_type"), QStringLiteral("annotation")},
                    {QStringLiteral("payload"), QJsonObject{{QStringLiteral("subtype"), QStringLiteral("Highlight")}}}},
        QJsonObject{{QStringLiteral("entity_type"), QStringLiteral("annotation")},
                    {QStringLiteral("payload"), QJsonObject()}},
        QJsonObject{{QStringLiteral("entity_type"), QStringLiteral("review_event")},
                    {QStringLiteral("payload"), QJsonObject()}},
    };
    const QList<QJsonObject> normalized = Thoughts::SyncClient::normalizedOperationsForUpload(operations);
    QCOMPARE(normalized.at(0).value(QStringLiteral("payload")).toObject().value(QStringLiteral("subtype")).toString(), QStringLiteral("Text"));
    QCOMPARE(normalized.at(1).value(QStringLiteral("payload")).toObject().value(QStringLiteral("subtype")).toString(), QStringLiteral("Highlight"));
    QCOMPARE(normalized.at(2).value(QStringLiteral("payload")).toObject().value(QStringLiteral("subtype")).toString(), QStringLiteral("Text"));
    QVERIFY(!normalized.at(3).value(QStringLiteral("payload")).toObject().contains(QStringLiteral("subtype")));
}

void ClientTest::structuredErrorDetails()
{
    const QByteArray body = R"({"detail":[{"type":"string_too_short","loc":["body","operations",0,"subtype"],"msg":"String should have at least 1 character"}]})";
    const QList<QJsonObject> operations {QJsonObject{{QStringLiteral("operation_id"), QStringLiteral("operation-1")},
                                                     {QStringLiteral("entity_type"), QStringLiteral("annotation")},
                                                     {QStringLiteral("collection"), QStringLiteral("paper")},
                                                     {QStringLiteral("payload"), QJsonObject{{QStringLiteral("contents"), QStringLiteral("private comment")}}}}};
    const Thoughts::SyncErrorInfo error = Thoughts::SyncClient::parseErrorResponse(body, QStringLiteral("generic error"), 422, operations);
    QCOMPARE(error.httpStatus, 422);
    QCOMPARE(error.summary, QStringLiteral("A queued annotation has an empty subtype. Your changes remain safely queued."));
    QVERIFY(error.details.contains(QStringLiteral("Server HTTP 422")));
    QVERIFY(error.details.contains(QStringLiteral("Queued change 1 of 1")));
    QVERIFY(error.details.contains(QStringLiteral("Collection: paper")));
    QVERIFY(error.details.contains(QStringLiteral("Operation ID: operation-1")));
    QVERIFY(error.details.contains(QStringLiteral("Field: payload.subtype")));
    QVERIFY(error.details.contains(QStringLiteral("String should have at least 1 character")));
    QVERIFY(!error.details.contains(QStringLiteral("private comment")));

    const Thoughts::SyncErrorInfo offline = Thoughts::SyncClient::parseErrorResponse({}, QStringLiteral("Host not found"), 0);
    QCOMPARE(offline.summary, QStringLiteral("Could not reach the server. Your changes remain safely queued."));
    QCOMPARE(offline.details, QStringLiteral("Host not found"));

    const Thoughts::SyncErrorInfo stringDetail = Thoughts::SyncClient::parseErrorResponse(
        QByteArrayLiteral("{\"detail\":\"The supplied cursor is stale.\"}"), QStringLiteral("generic error"), 409);
    QCOMPARE(stringDetail.summary, QStringLiteral("The supplied cursor is stale."));
    QVERIFY(stringDetail.details.contains(QStringLiteral("Server HTTP 409")));

    const Thoughts::SyncErrorInfo unknownHttp = Thoughts::SyncClient::parseErrorResponse(
        QByteArrayLiteral("not json"), QStringLiteral("Internal Server Error"), 500);
    QCOMPARE(unknownHttp.summary, QStringLiteral("The server rejected the sync request (HTTP 500). Your changes remain safely queued."));
    QCOMPARE(unknownHttp.details, QStringLiteral("Internal Server Error"));
}

void ClientTest::syncStateRequest()
{
    QTcpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost));
    QUrl baseUrl;
    baseUrl.setScheme(QStringLiteral("http"));
    baseUrl.setHost(QStringLiteral("127.0.0.1"));
    baseUrl.setPort(server.serverPort());
    Thoughts::SyncClient client(baseUrl, QByteArrayLiteral("token"));
    client.setTraceId(QByteArrayLiteral("11111111-1111-4111-8111-111111111111"));

    QNetworkReply *reply = client.syncState();
    QVERIFY(reply != nullptr);
    QSignalSpy finishedSpy(reply, &QNetworkReply::finished);
    QTRY_VERIFY_WITH_TIMEOUT(server.hasPendingConnections(), 5000);
    QTcpSocket *socket = server.nextPendingConnection();
    QVERIFY(socket != nullptr);
    QByteArray request;
    const auto receivedCompleteRequest = [&request, socket]() {
        request.append(socket->readAll());
        return completeHttpRequest(request);
    };
    QTRY_VERIFY_WITH_TIMEOUT(receivedCompleteRequest(), 5000);

    const qsizetype firstLineEnd = request.indexOf(QByteArrayLiteral("\r\n"));
    QCOMPARE(request.left(firstLineEnd), QByteArrayLiteral("GET /v2/sync/state HTTP/1.1"));
    QVERIFY(request.contains(QByteArrayLiteral("Authorization: Bearer token\r\n")));
    QVERIFY(request.contains(QByteArrayLiteral(
        "X-Thoughts-Trace-Id: 11111111-1111-4111-8111-111111111111\r\n")));

    const QByteArray responseBody = QByteArrayLiteral(
        "{\"contract_version\":2,\"latest_server_sequence\":7,"
        "\"library_revision\":\"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\","
        "\"source_frontier\":4}");
    const QByteArray response = QByteArrayLiteral(
                                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ")
        + QByteArray::number(responseBody.size())
        + QByteArrayLiteral("\r\nConnection: close\r\n\r\n") + responseBody;
    QCOMPARE(socket->write(response), response.size());
    socket->flush();
    QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.size(), 1, 5000);
    QCOMPARE(reply->error(), QNetworkReply::NoError);
    QCOMPARE(reply->readAll(), responseBody);
}

void ClientTest::redirectToAnotherOriginIsNotFollowed()
{
    // Every request carries the device token, so a redirect to another origin (here: another
    // port) must end the request instead of sending the token there.
    QTcpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost));
    QTcpServer otherOrigin;
    QVERIFY(otherOrigin.listen(QHostAddress::LocalHost));
    QUrl baseUrl;
    baseUrl.setScheme(QStringLiteral("http"));
    baseUrl.setHost(QStringLiteral("127.0.0.1"));
    baseUrl.setPort(server.serverPort());
    Thoughts::SyncClient client(baseUrl, QByteArrayLiteral("token"));

    QNetworkReply *reply = client.syncState();
    QVERIFY(reply != nullptr);
    QSignalSpy finishedSpy(reply, &QNetworkReply::finished);
    QTRY_VERIFY_WITH_TIMEOUT(server.hasPendingConnections(), 5000);
    QTcpSocket *socket = server.nextPendingConnection();
    QVERIFY(socket != nullptr);
    QByteArray request;
    const auto receivedCompleteRequest = [&request, socket]() {
        request.append(socket->readAll());
        return completeHttpRequest(request);
    };
    QTRY_VERIFY_WITH_TIMEOUT(receivedCompleteRequest(), 5000);

    const QByteArray response = QByteArrayLiteral("HTTP/1.1 307 Temporary Redirect\r\nLocation: http://127.0.0.1:")
        + QByteArray::number(otherOrigin.serverPort())
        + QByteArrayLiteral("/v2/sync/state\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
    QCOMPARE(socket->write(response), response.size());
    socket->flush();
    QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.size(), 1, 5000);
    QVERIFY(reply->error() != QNetworkReply::NoError);
    QTest::qWait(200);
    QVERIFY(!otherOrigin.hasPendingConnections());
}

void ClientTest::plannedObjectRequest_data()
{
    QTest::addColumn<bool>("sourceBundleObject");

    QTest::newRow("source-artifact") << true;
    QTest::newRow("planned-object") << false;
}

void ClientTest::objectMetadataRequest()
{
    QTcpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost));
    QUrl baseUrl;
    baseUrl.setScheme(QStringLiteral("http"));
    baseUrl.setHost(QStringLiteral("127.0.0.1"));
    baseUrl.setPort(server.serverPort());
    Thoughts::SyncClient client(baseUrl, QByteArrayLiteral("token"));
    const QByteArray digest(64, 'd');

    QNetworkReply *reply = client.objectMetadata(digest);
    QVERIFY(reply != nullptr);
    QSignalSpy finishedSpy(reply, &QNetworkReply::finished);
    QTRY_VERIFY_WITH_TIMEOUT(server.hasPendingConnections(), 5000);
    QTcpSocket *socket = server.nextPendingConnection();
    QVERIFY(socket != nullptr);
    QByteArray request;
    const auto receivedCompleteRequest = [&request, socket]() {
        request.append(socket->readAll());
        return completeHttpRequest(request);
    };
    QTRY_VERIFY_WITH_TIMEOUT(receivedCompleteRequest(), 5000);

    const qsizetype firstLineEnd = request.indexOf(QByteArrayLiteral("\r\n"));
    QCOMPARE(request.left(firstLineEnd), QByteArrayLiteral("HEAD /v1/objects/") + digest + QByteArrayLiteral(" HTTP/1.1"));
    QVERIFY(request.contains(QByteArrayLiteral("Authorization: Bearer token\r\n")));

    const QByteArray response = QByteArrayLiteral("HTTP/1.1 200 OK\r\nContent-Length: 123\r\nETag: \"") + digest
        + QByteArrayLiteral("\"\r\nConnection: close\r\n\r\n");
    QCOMPARE(socket->write(response), response.size());
    socket->flush();
    QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.size(), 1, 5000);
    QCOMPARE(reply->error(), QNetworkReply::NoError);
    QCOMPARE(reply->rawHeader(QByteArrayLiteral("Content-Length")), QByteArrayLiteral("123"));
    QCOMPARE(reply->rawHeader(QByteArrayLiteral("ETag")), QByteArrayLiteral("\"") + digest + QByteArrayLiteral("\""));
    QCOMPARE(reply->readAll(), QByteArray());
}

void ClientTest::plannedObjectRequest()
{
    QFETCH(bool, sourceBundleObject);
    QTcpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost));
    QUrl baseUrl;
    baseUrl.setScheme(QStringLiteral("http"));
    baseUrl.setHost(QStringLiteral("127.0.0.1"));
    baseUrl.setPort(server.serverPort());
    Thoughts::SyncClient client(baseUrl, QByteArrayLiteral("token"));
    const QByteArray digest(64, 'a');
    const QByteArray planId(64, 'b');

    QNetworkReply *reply = sourceBundleObject
        ? client.sourceBundleObject(digest, planId)
        : client.downloadObject(digest, planId);
    QVERIFY(reply != nullptr);
    QSignalSpy finishedSpy(reply, &QNetworkReply::finished);
    QTRY_VERIFY_WITH_TIMEOUT(server.hasPendingConnections(), 5000);
    QTcpSocket *socket = server.nextPendingConnection();
    QVERIFY(socket != nullptr);
    QByteArray request;
    const auto receivedCompleteRequest = [&request, socket]() {
        request.append(socket->readAll());
        return completeHttpRequest(request);
    };
    QTRY_VERIFY_WITH_TIMEOUT(receivedCompleteRequest(), 5000);

    const qsizetype firstLineEnd = request.indexOf(QByteArrayLiteral("\r\n"));
    QCOMPARE(request.left(firstLineEnd), QByteArrayLiteral("GET /v2/artifacts/") + digest + QByteArrayLiteral(" HTTP/1.1"));
    QVERIFY(request.contains(QByteArray(QByteArrayLiteral("X-Thoughts-Sync-Plan: ") + planId + QByteArrayLiteral("\r\n"))));

    const QByteArray responseBody = QByteArrayLiteral("planned object");
    const QByteArray response = QByteArrayLiteral("HTTP/1.1 200 OK\r\nContent-Length: ")
        + QByteArray::number(responseBody.size())
        + QByteArrayLiteral("\r\nConnection: close\r\n\r\n") + responseBody;
    QCOMPARE(socket->write(response), response.size());
    socket->flush();
    QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.size(), 1, 5000);
    QCOMPARE(reply->error(), QNetworkReply::NoError);
    QCOMPARE(reply->readAll(), responseBody);
}

void ClientTest::unplannedObjectRequestUsesGenericRoute()
{
    QTcpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost));
    QUrl baseUrl;
    baseUrl.setScheme(QStringLiteral("http"));
    baseUrl.setHost(QStringLiteral("127.0.0.1"));
    baseUrl.setPort(server.serverPort());
    Thoughts::SyncClient client(baseUrl, QByteArrayLiteral("token"));
    const QByteArray digest(64, 'c');
    QVERIFY(client.sourceBundleObject(digest, {}) == nullptr);

    QNetworkReply *reply = client.downloadObject(digest);
    QVERIFY(reply != nullptr);
    QSignalSpy finishedSpy(reply, &QNetworkReply::finished);
    QTRY_VERIFY_WITH_TIMEOUT(server.hasPendingConnections(), 5000);
    QTcpSocket *socket = server.nextPendingConnection();
    QVERIFY(socket != nullptr);
    QByteArray request;
    const auto receivedCompleteRequest = [&request, socket]() {
        request.append(socket->readAll());
        return completeHttpRequest(request);
    };
    QTRY_VERIFY_WITH_TIMEOUT(receivedCompleteRequest(), 5000);

    const qsizetype firstLineEnd = request.indexOf(QByteArrayLiteral("\r\n"));
    QCOMPARE(request.left(firstLineEnd), QByteArrayLiteral("GET /v1/objects/") + digest + QByteArrayLiteral(" HTTP/1.1"));
    QVERIFY(!request.contains(QByteArrayLiteral("X-Thoughts-Sync-Plan:")));

    const QByteArray response = QByteArrayLiteral(
        "HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
    QCOMPARE(socket->write(response), response.size());
    socket->flush();
    QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.size(), 1, 5000);
    QCOMPARE(reply->error(), QNetworkReply::NoError);
}

static bool completeHttpRequest(const QByteArray &request)
{
    const qsizetype headerEnd = request.indexOf(QByteArrayLiteral("\r\n\r\n"));
    if (headerEnd < 0) {
        return false;
    }
    qint64 contentLength = 0;
    for (const QByteArray &line : request.left(headerEnd).split('\n')) {
        const QByteArray trimmed = line.trimmed();
        if (trimmed.toLower().startsWith(QByteArrayLiteral("content-length:"))) {
            bool ok = false;
            contentLength = trimmed.mid(qsizetype(QByteArrayLiteral("content-length:").size())).trimmed().toLongLong(&ok);
            if (!ok || contentLength < 0) {
                return false;
            }
            break;
        }
    }
    return request.size() >= headerEnd + 4 + contentLength;
}

void ClientTest::renderCheckRequest_data()
{
    QTest::addColumn<QString>("target");
    QTest::addColumn<QString>("requestId");
    QTest::addColumn<QJsonObject>("expectedPayload");

    QTest::newRow("library-default")
        << QStringLiteral("all") << QString() << QJsonObject{{QStringLiteral("target"), QStringLiteral("all")}};
    QTest::newRow("targeted-idempotent")
        << QStringLiteral("paper.pdf") << QStringLiteral("sync-operation-7")
        << QJsonObject{{QStringLiteral("target"), QStringLiteral("paper.pdf")},
                       {QStringLiteral("request_id"), QStringLiteral("sync-operation-7")}};
}

void ClientTest::renderCheckRequest()
{
    QFETCH(QString, target);
    QFETCH(QString, requestId);
    QFETCH(QJsonObject, expectedPayload);

    QTcpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost));
    QUrl baseUrl;
    baseUrl.setScheme(QStringLiteral("http"));
    baseUrl.setHost(QStringLiteral("127.0.0.1"));
    baseUrl.setPort(server.serverPort());
    Thoughts::SyncClient client(baseUrl, QByteArrayLiteral("token"));
    client.setTraceId(QByteArrayLiteral("11111111-1111-4111-8111-111111111111"));

    QNetworkReply *reply = client.renderCheck(target, requestId);
    QVERIFY(reply != nullptr);
    QSignalSpy finishedSpy(reply, &QNetworkReply::finished);
    QTRY_VERIFY_WITH_TIMEOUT(server.hasPendingConnections(), 5000);
    QTcpSocket *socket = server.nextPendingConnection();
    QVERIFY(socket != nullptr);
    QByteArray request;
    const auto receivedCompleteRequest = [&request, socket]() {
        request.append(socket->readAll());
        return completeHttpRequest(request);
    };
    QTRY_VERIFY_WITH_TIMEOUT(receivedCompleteRequest(), 5000);

    const qsizetype firstLineEnd = request.indexOf(QByteArrayLiteral("\r\n"));
    QCOMPARE(request.left(firstLineEnd), QByteArrayLiteral("POST /v1/render-checks HTTP/1.1"));
    QVERIFY(request.contains(QByteArrayLiteral("Authorization: Bearer token\r\n")));
    QVERIFY(request.contains(QByteArrayLiteral("X-Thoughts-Trace-Id: 11111111-1111-4111-8111-111111111111\r\n")));
    QVERIFY(request.contains(QByteArrayLiteral("Content-Type: application/json\r\n")));
    const qsizetype headerEnd = request.indexOf(QByteArrayLiteral("\r\n\r\n"));
    QCOMPARE(QJsonDocument::fromJson(request.mid(headerEnd + 4)).object(), expectedPayload);

    const QByteArray responseBody = QByteArrayLiteral("{\"status\":\"current\",\"library_revision\":12}");
    const QByteArray response = QByteArrayLiteral("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ")
        + QByteArray::number(responseBody.size()) + QByteArrayLiteral("\r\nConnection: close\r\n\r\n") + responseBody;
    QCOMPARE(socket->write(response), response.size());
    socket->flush();
    QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.size(), 1, 5000);
    QCOMPARE(reply->error(), QNetworkReply::NoError);
    QCOMPARE(reply->readAll(), responseBody);
}

void ClientTest::discardConflictRequest()
{
    QTcpServer server;
    QVERIFY(server.listen(QHostAddress::LocalHost));
    QUrl baseUrl;
    baseUrl.setScheme(QStringLiteral("http"));
    baseUrl.setHost(QStringLiteral("127.0.0.1"));
    baseUrl.setPort(server.serverPort());
    Thoughts::SyncClient client(baseUrl, QByteArrayLiteral("token"));
    const QJsonObject payload{{QStringLiteral("expected_revision"), 7},
                              {QStringLiteral("expected_sha256"), QString(64, QLatin1Char('a'))}};

    QNetworkReply *reply = client.discardConflict(QStringLiteral("conflict-1"), payload);
    QVERIFY(reply != nullptr);
    QSignalSpy finishedSpy(reply, &QNetworkReply::finished);
    QTRY_VERIFY_WITH_TIMEOUT(server.hasPendingConnections(), 5000);
    QTcpSocket *socket = server.nextPendingConnection();
    QVERIFY(socket != nullptr);
    QByteArray request;
    const auto receivedCompleteRequest = [&request, socket]() {
        request.append(socket->readAll());
        return completeHttpRequest(request);
    };
    QTRY_VERIFY_WITH_TIMEOUT(receivedCompleteRequest(), 5000);
    const qsizetype firstLineEnd = request.indexOf(QByteArrayLiteral("\r\n"));
    QCOMPARE(request.left(firstLineEnd), QByteArrayLiteral("POST /v1/conflicts/conflict-1/discard HTTP/1.1"));
    const qsizetype headerEnd = request.indexOf(QByteArrayLiteral("\r\n\r\n"));
    QCOMPARE(QJsonDocument::fromJson(request.mid(headerEnd + 4)).object(), payload);

    const QByteArray responseBody = QByteArrayLiteral("{\"id\":\"conflict-1\",\"status\":\"discarded\"}");
    const QByteArray response = QByteArrayLiteral("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ")
        + QByteArray::number(responseBody.size()) + QByteArrayLiteral("\r\nConnection: close\r\n\r\n") + responseBody;
    QCOMPARE(socket->write(response), response.size());
    socket->flush();
    QTRY_COMPARE_WITH_TIMEOUT(finishedSpy.size(), 1, 5000);
    QCOMPARE(reply->error(), QNetworkReply::NoError);
}

void ClientTest::sharedTraceSseFixture()
{
    QFile file(QStringLiteral(THOUGHTS_PROTOCOL_FIXTURE_DIR "/trace_events_v1.json"));
    QVERIFY(file.open(QIODevice::ReadOnly));
    const QJsonObject fixture = QJsonDocument::fromJson(file.readAll()).object();
    Thoughts::TraceSseParser parser;
    QList<Thoughts::TraceSseFrame> frames;
    for (const QJsonValue &value : fixture.value(QStringLiteral("chunks")).toArray()) {
        const QByteArray chunk = value.toString().toUtf8();
        const qsizetype split = chunk.size() / 2;
        frames.append(parser.feed(chunk.left(split)));
        frames.append(parser.feed(chunk.mid(split)));
    }
    frames.append(parser.finish());
    QList<qint64> sequences;
    qint64 retry = -1;
    QByteArray heartbeat;
    QString terminalMessage;
    for (const Thoughts::TraceSseFrame &frame : std::as_const(frames)) {
        if (frame.retryMillis >= 0) {
            retry = frame.retryMillis;
        }
        if (!frame.comment.isEmpty()) {
            heartbeat = frame.comment;
        }
        if (frame.event == QByteArrayLiteral("trace")) {
            const QJsonObject event = QJsonDocument::fromJson(frame.data).object();
            sequences.append(event.value(QStringLiteral("sequence")).toInteger());
            if (event.value(QStringLiteral("terminal")).toBool()) {
                terminalMessage = event.value(QStringLiteral("message")).toString();
            }
        }
    }
    QCOMPARE(retry, fixture.value(QStringLiteral("expected_retry_ms")).toInteger());
    QCOMPARE(QString::fromUtf8(heartbeat), fixture.value(QStringLiteral("expected_heartbeat")).toString());
    QCOMPARE(sequences, QList<qint64>({1, 2}));
    QCOMPARE(terminalMessage, fixture.value(QStringLiteral("expected_terminal_message")).toString());
}

void ClientTest::sharedSyncTransferPlanFixture()
{
    QCOMPARE(QString::fromLatin1(Thoughts::SourceResourceCapability),
             QStringLiteral("source_resource_v1"));
    QFile file(QStringLiteral(THOUGHTS_PROTOCOL_FIXTURE_DIR "/sync-transfer-plan-v2.json"));
    QVERIFY(file.open(QIODevice::ReadOnly));
    const QJsonObject fixture = QJsonDocument::fromJson(file.readAll()).object();
    const QJsonObject plan = fixture.value(QStringLiteral("plan")).toObject();
    QCOMPARE(plan.value(QStringLiteral("contract_version")).toInt(), 2);
    QCOMPARE(plan.value(QStringLiteral("source_profile")).toString(), QStringLiteral("reader-v1"));
    QVERIFY(!plan.value(QStringLiteral("include_voice_media")).toBool());

    QJsonArray identities;
    qint64 exactBytes = plan.value(QStringLiteral("metadata_download_bytes")).toInteger();
    for (const QJsonValue &value : plan.value(QStringLiteral("objects")).toArray()) {
        const QJsonObject object = value.toObject();
        QStringList roles;
        for (const QJsonValue &role : object.value(QStringLiteral("roles")).toArray()) {
            roles.append(role.toString());
        }
        QStringList publications;
        for (const QJsonValue &publication : object.value(QStringLiteral("publications")).toArray()) {
            publications.append(publication.toString());
        }
        identities.append(QString(
            object.value(QStringLiteral("sha256")).toString() + QLatin1Char(':')
            + object.value(QStringLiteral("kind")).toString() + QLatin1Char(':')
            + roles.join(QLatin1Char(',')) + QLatin1Char(':')
            + publications.join(QLatin1Char(',')) + QLatin1Char(':')
            + QString::number(object.value(QStringLiteral("size_bytes")).toInteger())));
        exactBytes += object.value(QStringLiteral("size_bytes")).toInteger();
    }
    QCOMPARE(identities, fixture.value(QStringLiteral("selected_object_identities")).toArray());
    QCOMPARE(exactBytes, plan.value(QStringLiteral("download_bytes")).toInteger());
}

static QString reconciledState(const QJsonObject &facts)
{
    if (!facts.value(QStringLiteral("terminal")).toString().isEmpty()) {
        return facts.value(QStringLiteral("terminal")).toString();
    }
    if (facts.value(QStringLiteral("activation_complete")).toBool()) {
        return QStringLiteral("succeeded");
    }
    if (facts.contains(QStringLiteral("network_available")) && !facts.value(QStringLiteral("network_available")).toBool()) {
        return QStringLiteral("waiting_network");
    }
    if (facts.value(QStringLiteral("waiting_for_user")).toBool()) {
        return QStringLiteral("waiting_user");
    }
    if (!facts.value(QStringLiteral("preflight_complete")).toBool()) {
        return QStringLiteral("preflight");
    }
    if (!facts.value(QStringLiteral("upload_acknowledged")).toBool()) {
        return QStringLiteral("uploading");
    }
    if (facts.value(QStringLiteral("build_required")).toBool() && !facts.value(QStringLiteral("build_complete")).toBool()) {
        return QStringLiteral("building");
    }
    if (!facts.value(QStringLiteral("download_complete")).toBool()) {
        return QStringLiteral("downloading");
    }
    if (!facts.value(QStringLiteral("verification_complete")).toBool()) {
        return QStringLiteral("verifying");
    }
    return QStringLiteral("activating");
}

void ClientTest::sharedReconcilerTransitions()
{
    QFile file(QStringLiteral(THOUGHTS_PROTOCOL_FIXTURE_DIR "/sync-reconciler-transitions.json"));
    QVERIFY(file.open(QIODevice::ReadOnly));
    const QJsonObject fixture = QJsonDocument::fromJson(file.readAll()).object();
    QCOMPARE(fixture.value(QStringLiteral("schema_version")).toInt(), 1);
    for (const QJsonValue &value : fixture.value(QStringLiteral("cases")).toArray()) {
        const QJsonObject testCase = value.toObject();
        QCOMPARE(reconciledState(testCase.value(QStringLiteral("facts")).toObject()),
                 testCase.value(QStringLiteral("expected")).toString());
    }
}

QTEST_MAIN(ClientTest)
#include "clienttest.moc"
