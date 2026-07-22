require 'rails_helper'

RSpec.describe Client, type: :model do
  describe 'validations' do
    it 'is valid with a name and email' do
      expect(build(:client)).to be_valid
    end

    it 'is invalid without a name' do
      client = build(:client, name: nil)
      expect(client).not_to be_valid
      expect(client.errors[:name]).to include("can't be blank")
    end

    it 'is invalid without an email' do
      client = build(:client, email: nil)
      expect(client).not_to be_valid
      expect(client.errors[:email]).to include("can't be blank")
    end

    it 'is invalid with a duplicate email' do
      create(:client, email: 'john@example.com')
      duplicate = build(:client, email: 'john@example.com')
      expect(duplicate).not_to be_valid
      expect(duplicate.errors[:email]).to include('has already been taken')
    end

    it 'is invalid with a malformed email' do
      client = build(:client, email: 'notanemail')
      expect(client).not_to be_valid
      expect(client.errors[:email]).to include('is invalid')
    end
  end

  describe 'associations' do
    it 'has many bookings' do
      client = create(:client)
      create(:booking, client: client)
      expect(client.bookings.count).to eq(1)
    end

    it 'destroys associated bookings when deleted' do
      client = create(:client)
      create(:booking, client: client)
      expect { client.destroy }.to change(Booking, :count).by(-1)
    end
  end
end
