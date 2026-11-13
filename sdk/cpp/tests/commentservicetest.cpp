// SPDX-License-Identifier: GPL-2.0-or-later
// SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
#include "thoughts/commentservice.h"
#include <QFile>
#include <QJsonDocument>
#include <QJsonArray>
#include <QTest>
class CommentServiceTest : public QObject {
    Q_OBJECT
private Q_SLOTS:
    void sharedLifecycle() {
        QFile file(QStringLiteral(THOUGHTS_PROTOCOL_FIXTURE_DIR "/comment-lifecycle.json"));
        QVERIFY(file.open(QIODevice::ReadOnly));
        for (const auto &encoded : QJsonDocument::fromJson(file.readAll()).object().value(QStringLiteral("cases")).toArray()) {
            const auto item = encoded.toObject();
            QList<Thoughts::CommentRecord> records;
            for (const auto &value : item.value(QStringLiteral("records")).toArray()) {
                const auto input = value.toObject(); const auto id = input.value(QStringLiteral("id")).toString();
                QJsonObject payload;
                payload.insert(QStringLiteral("comment_id"), id);
                payload.insert(QStringLiteral("revision"), 5);
                payload.insert(QStringLiteral("contents"), QString(QStringLiteral("Original ") + id));
                payload.insert(QStringLiteral("deleted"), input.value(QStringLiteral("deleted")).toBool());
                payload.insert(QStringLiteral("custom_metadata"), QJsonObject{{QStringLiteral("preserve"), true}});
                records.append(Thoughts::CommentRecord{id, input.value(QStringLiteral("parent")).toString(), payload});
            }
            int commits = 0;
            Thoughts::CommentService service([&] { return records; }, [&](const auto &changes, QString *) {
                ++commits;
                for (const auto &change : changes) for (auto &record : records) if (record.id == change.after.id) record = change.after;
                return true;
            }, [] { return QStringLiteral("2026-09-12T12:00:00Z"); }, QStringLiteral("test"));
            QStringList selected, expected;
            for (const auto &id : item.value(QStringLiteral("selected")).toArray()) selected.append(id.toString());
            for (const auto &id : item.value(QStringLiteral("deleted")).toArray()) expected.append(id.toString());
            const auto result = service.deleteComments(selected);
            QCOMPARE(result.success, item.value(QStringLiteral("success")).toBool(true));
            QCOMPARE(result.ids, expected);
            if (!result.success) { QCOMPARE(commits, 0); continue; }
            QCOMPARE(commits, 1);
            // Explicit deletes name their reason; automatic tombstones never carry it.
            for (const auto &record : records) {
                if (result.ids.contains(record.id)) QCOMPARE(record.payload.value(QStringLiteral("deletion_reason")).toString(), Thoughts::CommentService::userDeletionReason());
                else QVERIFY(!record.payload.contains(QStringLiteral("deletion_reason")));
            }
            QCOMPARE(Thoughts::CommentService::userDeletionReason(), QStringLiteral("user_deleted"));
            QVERIFY(service.deleteComments(result.ids).ids.isEmpty());
            for (auto &record : records) if (record.id == result.ids.first()) { record.payload.insert(QStringLiteral("contents"), QStringLiteral("Newer edit")); record.payload.insert(QStringLiteral("revision"), 20); }
            QVERIFY(service.undo(result.historyToken).success);
            for (const auto &record : records) if (record.id == result.ids.first()) { QCOMPARE(record.payload.value(QStringLiteral("contents")).toString(), QStringLiteral("Newer edit")); QCOMPARE(record.payload.value(QStringLiteral("revision")).toInt(), 21); }
            // A restored comment is live and carries no deletion reason.
            for (const auto &record : records) if (result.ids.contains(record.id)) QVERIFY(!record.payload.contains(QStringLiteral("deletion_reason")));
            QVERIFY(!service.undo(result.historyToken).success);
            QVERIFY(service.redo(result.historyToken).success);
            for (const auto &record : records) if (result.ids.contains(record.id)) {
                QVERIFY(record.payload.value(QStringLiteral("deleted")).toBool());
                QCOMPARE(record.payload.value(QStringLiteral("deletion_reason")).toString(), QStringLiteral("user_deleted"));
                QCOMPARE(record.payload.value(QStringLiteral("custom_metadata")).toObject().value(QStringLiteral("preserve")).toBool(), true);
            }
        }
    }
    void failedCommit() {
        Thoughts::CommentService service([] { return QList<Thoughts::CommentRecord>{{QStringLiteral("note"), {}, {}}}; }, [](const auto &, QString *error) { *error = QStringLiteral("disk full"); return false; }, [] { return QStringLiteral("now"); }, QStringLiteral("test"));
        const auto result = service.deleteComments({QStringLiteral("note")});
        QVERIFY(!result.success); QVERIFY(result.ids.isEmpty()); QCOMPARE(result.historyToken, 0);
        QVERIFY(!service.undo(1).success);
    }
};
QTEST_GUILESS_MAIN(CommentServiceTest)
#include "commentservicetest.moc"
