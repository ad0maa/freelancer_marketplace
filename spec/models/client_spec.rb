require 'rails_helper'

RSpec.describe Client, type: :model do
  describe 'validations' do
    it 'is valid with a name and email' do
      client = described_class.new(
        name: 'John Doe',
        email: 'john@example.com'
      )
      expect(client).to be_valid
    end

    it 'is invalid without a name' do
      client = described_class.new(name: nil, email: 'john@example.com')
      expect(client).not_to be_valid
      expect(client.errors[:name]).to include("can't be blank")
    end

    it 'is invalid without an email' do
      client = described_class.new(name: 'John', email: nil)
      expect(client).not_to be_valid
      expect(client.errors[:email]).to include("can't be blank")
    end

    it 'is invalid with a duplicate email' do
      described_class.create!(name: 'John', email: 'john@example.com')
      duplicate = described_class.new(name: 'Other John', email: 'john@example.com')
      expect(duplicate).not_to be_valid
      expect(duplicate.errors[:email]).to include('has already been taken')
    end
  end
end
